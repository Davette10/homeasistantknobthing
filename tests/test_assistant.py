import asyncio
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from assistant.agent import Agent, ThinkFilter
from assistant.config import Settings
from assistant.db import Store
from assistant.scheduler import Notifier, Scheduler
from assistant.timeparse import TimeParseError, parse_recurrence, parse_when
from assistant.tools import Toolbox
from assistant.web import create_app

TZ = ZoneInfo("America/New_York")
UTC = timezone.utc
NOW = datetime(2026, 9, 26, 15, 4, tzinfo=TZ)  # Saturday 3:04 PM


def local(dt):
    return dt.astimezone(TZ).replace(tzinfo=None)


@pytest.fixture
def settings(tmp_path):
    persona = tmp_path / "persona.md"
    persona.write_text("Be nice.")
    return Settings(web_password="hunter22", secret_key="k" * 32, db_path=tmp_path / "t.db", persona_path=persona)


@pytest.fixture
def store(settings):
    return Store(settings.db_path)


# --- time parsing -------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("in 20 minutes", datetime(2026, 9, 26, 15, 24)),
    ("tomorrow at 5pm", datetime(2026, 9, 27, 17, 0)),
    ("5pm", datetime(2026, 9, 26, 17, 0)),
    ("8am", datetime(2026, 9, 27, 8, 0)),           # already passed today -> tomorrow
    ("noon", datetime(2026, 9, 27, 12, 0)),
    ("next monday at 9", datetime(2026, 9, 28, 9, 0)),
    ("monday", datetime(2026, 9, 28, 9, 0)),        # no clock time -> 9am
    ("tonight", datetime(2026, 9, 26, 20, 0)),
    ("tomorrow morning", datetime(2026, 9, 27, 9, 0)),
    ("2026-10-01 14:30", datetime(2026, 10, 1, 14, 30)),
    ("tonight at 8", datetime(2026, 9, 26, 20, 0)),
    ("tomorrow morning at 7", datetime(2026, 9, 27, 7, 0)),
    ("this evening at 6:30", datetime(2026, 9, 26, 18, 30)),
    ("at 8", datetime(2026, 9, 26, 20, 0)),          # bare hour -> soonest sensible one
    ("at 3", datetime(2026, 9, 27, 15, 0)),          # never 3am
    ("tomorrow", datetime(2026, 9, 27, 9, 0)),
    ("oct 12 at 4", datetime(2026, 10, 12, 16, 0)),
    ("in half an hour", datetime(2026, 9, 26, 15, 34)),
])
def test_parse_when(text, expected):
    assert local(parse_when(text, TZ, NOW)) == expected


@pytest.mark.parametrize("text", ["", "asdf qwerty", "2020-01-01 10:00"])
def test_parse_when_rejects(text):
    with pytest.raises(TimeParseError):
        parse_when(text, TZ, NOW)


def test_weekday_recurrence_skips_weekend():
    rec = parse_recurrence("weekdays")
    first = rec.first(parse_when("8am", TZ, NOW), TZ)  # asked on a Saturday
    assert local(first) == datetime(2026, 9, 28, 8, 0)  # Monday
    nxt = rec.next_after(first, first, TZ)
    assert local(nxt) == datetime(2026, 9, 29, 8, 0)


def test_daily_recurrence_keeps_wall_clock_across_dst():
    rec = parse_recurrence("daily")
    before = datetime(2026, 10, 31, 8, 0, tzinfo=TZ).astimezone(UTC)  # DST ends Nov 1
    nxt = rec.next_after(before, before, TZ)
    nxt2 = rec.next_after(nxt, nxt, TZ)
    assert local(nxt2) == datetime(2026, 11, 2, 8, 0)


def test_recurrence_parsing_variants():
    assert parse_recurrence("none") is None
    assert parse_recurrence(None) is None
    assert parse_recurrence("every week", days="mon, thu").weekdays == [0, 3]
    assert parse_recurrence("none", days=["fri"]).weekdays == [4]
    assert parse_recurrence("weekly", every="2").interval == 2
    assert parse_recurrence("biweekly").interval == 2
    with pytest.raises(TimeParseError):
        parse_recurrence("fortnightly-ish")


# --- tools --------------------------------------------------------------------

class SyncToolbox:
    """Calls the async toolbox synchronously, to keep the tests readable."""

    def __init__(self, store, settings):
        self.tb = Toolbox(store, settings)

    def run(self, name, args):
        return asyncio.run(self.tb.run(name, args))


def test_tools_roundtrip(store, settings):
    tb = SyncToolbox(store, settings)
    out = tb.run("set_reminder", {"text": "call mom", "when": "in 2 hours"})
    assert out.startswith("Reminder #1 set")
    assert "call mom" in tb.run("list_reminders", {})
    assert "Cancelled" in tb.run("cancel_reminder", {"id": "1"})  # string id is fine
    assert tb.run("list_reminders", {}) == "No upcoming reminders."

    assert "groceries" in tb.run("add_todo", {"item": "milk", "list": "Grocery list"})
    tb.run("add_todo", {"item": "file taxes"})
    listing = tb.run("list_todos", {})
    assert "groceries:" in listing and "to-do:" in listing
    assert "Checked off" in tb.run("complete_todo", {"id": 1})
    assert "milk" not in tb.run("list_todos", {})

    assert "Saved memory #1" in tb.run("remember", {"fact": "Sister Maya's birthday is May 3"})
    assert "Saved memory #1" in tb.run("remember", {"fact": "sister maya's birthday is may 3"})  # dedup
    assert "Forgot" in tb.run("forget", {"id": 1})


def test_tools_errors_are_messages(store, settings):
    tb = SyncToolbox(store, settings)
    assert tb.run("nope", {}).startswith("Error")
    assert tb.run("set_reminder", {"text": "x"}).startswith("Error")
    assert tb.run("set_reminder", {"text": "x", "when": "blorp"}).startswith("Error")
    assert tb.run("cancel_reminder", {"id": "abc"}).startswith("Error")
    assert tb.run("cancel_reminder", {"id": 99}).startswith("Error")
    assert tb.run("add_todo", '{"item": "json string args"}').startswith("Added")


# --- scheduler ----------------------------------------------------------------

def test_scheduler_fires_once_and_reschedules_repeats(store, settings):
    got = []
    notifier = Notifier()

    async def sink(r, late):
        got.append((r.id, late))
        return True

    notifier.add_sink(sink)
    sched = Scheduler(store, settings, notifier)
    now = datetime.now(UTC).replace(microsecond=0)
    one = store.add_reminder("one-off", now - timedelta(seconds=5))
    rep = store.add_reminder("daily", now - timedelta(hours=3), parse_recurrence("daily"))

    asyncio.run(sched.tick(now))
    assert sorted(got) == [(one.id, False), (rep.id, True)]
    assert store.get_reminder(one.id).status == "fired"
    assert store.get_reminder(rep.id).status == "pending"
    assert store.get_reminder(rep.id).due_at > now

    asyncio.run(sched.tick(now + timedelta(seconds=10)))
    assert len(got) == 2  # nothing fires twice

    snoozed = store.snooze_reminder(one.id, now + timedelta(minutes=10))
    assert snoozed.status == "pending"
    assert store.snooze_reminder(rep.id, now + timedelta(minutes=10)).id != rep.id  # copy for repeats


# --- agent loop with a fake Ollama -------------------------------------------

class FakeOllama:
    """Replays scripted responses; each script entry is one model round."""

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.seen = []

    async def stream_chat(self, messages, tools=None):
        self.seen.append([dict(m) for m in messages])
        for chunk in self.rounds.pop(0):
            yield chunk

    async def health(self):
        return {"ok": True, "model": "fake"}


def test_agent_runs_tool_then_answers(store, settings):
    fake = FakeOllama([
        [{"message": {"content": "", "tool_calls": [
            {"function": {"name": "set_reminder", "arguments": {"text": "stretch", "when": "in 30 minutes"}}}
        ]}, "done": True}],
        [{"message": {"content": "Done! I'll "}}, {"message": {"content": "nudge you."}, "done": True}],
    ])
    agent = Agent(settings, store, client=fake)

    async def run():
        return [ev async for ev in agent.chat("remind me to stretch in 30 min", "web")]

    events = asyncio.run(run())
    types = [e["type"] for e in events]
    assert types[0] == "tool" and types[-1] == "done"
    assert events[-1]["text"] == "Done! I'll nudge you."
    assert store.upcoming_reminders()[0].text == "stretch"
    # tool result was fed back to the model on the second round
    assert fake.seen[1][-1]["role"] == "tool" and "Reminder #1" in fake.seen[1][-1]["content"]
    # history persisted and fed into the next turn
    assert [m["role"] for m in store.recent_messages(10)] == ["user", "assistant"]


def test_system_prompt_includes_memories_and_time(store, settings):
    store.add_memory("Loves hiking")
    prompt = Agent(settings, store, client=FakeOllama([])).system_prompt(NOW)
    assert "Loves hiking" in prompt and "Saturday, September 26, 2026" in prompt and "Be nice." in prompt


def test_split_message_respects_discord_limit():
    from assistant.discord_bot import split_message

    text = "\n".join(f"line {i} " + "x" * 90 for i in range(60))
    chunks = split_message(text)
    assert all(len(c) <= 2000 for c in chunks) and "".join(chunks) == text
    assert split_message("y" * 4500) == ["y" * 2000, "y" * 2000, "y" * 500]


def test_think_filter_strips_split_tags():
    f = ThinkFilter()
    out = "".join(f.feed(p) for p in ["Hi <thi", "nk>secret plan</th", "ink> there"]) + f.flush()
    assert out == "Hi  there"


# --- web ----------------------------------------------------------------------

def test_web_auth_and_crud(store, settings):
    fake = FakeOllama([[{"message": {"content": "hey!"}, "done": True}]])
    app = create_app(settings, store, Agent(settings, store, client=fake), Notifier())
    c = TestClient(app)

    assert c.get("/").status_code == 200
    assert c.get("/api/me").json()["authed"] is False
    assert c.get("/api/todos").status_code == 401
    assert c.post("/api/login", json={"password": "wrong"}).status_code == 401
    assert c.post("/api/login", json={"password": "hunter22"}).status_code == 200
    assert c.get("/api/me").json()["authed"] is True

    t = c.post("/api/todos", json={"text": "bread", "list": "groceries"}).json()
    assert c.post(f"/api/todos/{t['id']}/toggle").json()["done"] is True
    r = c.post("/api/reminders", json={"text": "water plants", "when": "tomorrow 9am", "repeat": "weekly"}).json()
    assert r["repeat"] == "every week"
    assert c.get("/api/reminders").json()["upcoming"][0]["text"] == "water plants"
    assert c.post("/api/reminders", json={"text": "x", "when": "gibberish"}).status_code == 400
    c.post("/api/memories", json={"fact": "Has a dog named Biscuit"})
    assert c.get("/api/memories").json()[0]["fact"] == "Has a dog named Biscuit"

    lines = [json.loads(line) for line in c.post("/api/chat", json={"message": "hi"}).text.splitlines()]
    assert lines[-1] == {"type": "done", "text": "hey!"}
    assert len(c.get("/api/history").json()) == 2

    c.post("/api/logout")
    c.cookies.clear()
    assert c.get("/api/todos").status_code == 401
