import asyncio
import sqlite3
from datetime import datetime, time, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from assistant.agent import Agent, OllamaError
from assistant.coach import CheckinConfig, Coach, in_quiet_hours
from assistant.config import Settings
from assistant.db import Store, now_utc
from assistant.scheduler import Notifier
from assistant.tools import Toolbox
from assistant.web import create_app
from assistant.webtools import format_weather, page_text, parse_ddg_html, pick_place

UTC = timezone.utc


class DownOllama:
    """Model unreachable: the coach must fall back to its templates."""

    async def stream_chat(self, messages, tools=None):
        raise OllamaError("offline")
        yield  # pragma: no cover

    async def health(self):
        return {"ok": False, "error": "offline"}


class EchoOllama:
    """Replies with the instruction it was given, so tests can see what the model was asked."""

    def __init__(self):
        self.prompts = []

    async def stream_chat(self, messages, tools=None):
        self.prompts.append(messages[-1]["content"])
        yield {"message": {"content": "MODEL: " + messages[-1]["content"][:60]}, "done": True}


class FakeWeb:
    async def weather(self, place):
        return f"Weather for {place}:\nNow: 61°F (feels 60°F), partly cloudy, wind 5 mph\nToday: rain, high 66°F, low 52°F"


@pytest.fixture
def settings(tmp_path):
    return Settings(web_password="pw", secret_key="k" * 32, db_path=tmp_path / "t.db",
                    persona_path=tmp_path / "none.md", weather_location="Boston, MA")


@pytest.fixture
def store(settings):
    return Store(settings.db_path)


def make_coach(store, settings, client=None):
    notifier = Notifier()
    sent = []

    async def sink(text, kind, reminder_id):
        sent.append((kind, text, reminder_id))

    notifier.add_message_sink(sink)
    agent = Agent(settings, store, client=client or DownOllama())
    return Coach(store, settings, agent, notifier, web=FakeWeb()), sent


def at(settings, days_ahead: int, hh: int, mm: int = 0) -> datetime:
    """A moment `days_ahead` days from today, local time, as UTC."""
    local_day = datetime.now(settings.tz).date() + timedelta(days=days_ahead)
    return datetime.combine(local_day, time(hh, mm), tzinfo=settings.tz).astimezone(UTC)


def tick(coach, now):
    return asyncio.run(coach.tick(now))


# --- scheduled check-ins ------------------------------------------------------

def test_morning_brief_once_with_facts_and_weather(store, settings):
    coach, sent = make_coach(store, settings)
    store.add_todo("call the plumber")
    store.add_reminder("dentist", at(settings, 1, 15))

    assert tick(coach, at(settings, 1, 8, 5)) == "morning"
    kind, text, _ = sent[0]
    assert "Good morning" in text and "61°F" in text
    assert "dentist" in text and "call the plumber" in text
    assert tick(coach, at(settings, 1, 8, 6)) is None  # once a day
    # It's in the chat history, so the user's reply has context.
    assert store.recent_messages(1)[0]["source"] == "checkin"


def test_morning_brief_uses_model_when_available(store, settings):
    fake = EchoOllama()
    coach, sent = make_coach(store, settings, client=fake)
    tick(coach, at(settings, 1, 8, 0))
    assert sent[0][1].startswith("MODEL:")
    assert "morning brief" in fake.prompts[0] and "Weather:" in fake.prompts[0]


def test_nothing_after_the_window_or_in_quiet_hours(store, settings):
    coach, sent = make_coach(store, settings)
    assert tick(coach, at(settings, 1, 11, 30)) is None  # morning window (8-11) missed
    assert tick(coach, at(settings, 1, 23, 0)) is None    # quiet hours
    assert tick(coach, at(settings, 2, 6, 0)) is None
    assert sent == []


def test_mode_off_and_light(store, settings):
    coach, sent = make_coach(store, settings)
    CheckinConfig("off", "08:00", "13:00", "20:30", "22:00-07:30", "").save(store)
    assert tick(coach, at(settings, 1, 8, 0)) is None
    CheckinConfig("light", "08:00", "13:00", "20:30", "22:00-07:30", "").save(store)
    store.add_todo("x")
    assert tick(coach, at(settings, 1, 20, 30)) is None  # light mode has no evening check-in
    assert tick(coach, at(settings, 1, 8, 0)) == "morning"


def test_midday_only_when_something_is_open(store, settings):
    coach, sent = make_coach(store, settings)
    assert tick(coach, at(settings, 1, 13, 0)) is None
    store.add_todo("finish slides")
    assert tick(coach, at(settings, 2, 13, 0)) == "midday"
    assert "finish slides" in sent[-1][1]


def test_evening_asks_about_todays_steps(store, settings):
    coach, sent = make_coach(store, settings)
    g = store.add_goal("Run a 5K")
    store.add_goal_step(g.id, "run 1 mile", at(settings, 1, 9))
    assert tick(coach, at(settings, 1, 20, 30)) == "evening"
    assert "run 1 mile" in sent[-1][1]


# --- nudges -----------------------------------------------------------------

def test_overdue_step_nudged_once_then_gap(store, settings):
    coach, sent = make_coach(store, settings)
    g = store.add_goal("Learn guitar")
    step = store.add_goal_step(g.id, "practice chords", at(settings, 0, 9))  # due today
    # Not overdue on its own day...
    assert tick(coach, at(settings, 0, 23, 59) - timedelta(hours=2)) in (None, "evening")
    sent.clear()
    # ...but the next day it is. Morning brief first, then the nudge.
    assert tick(coach, at(settings, 1, 8, 0)) == "morning"
    assert tick(coach, at(settings, 1, 9, 0)) == "followup"
    assert "practice chords" in sent[-1][1]
    assert store.get_step(step.id).nudged_at is not None
    assert tick(coach, at(settings, 1, 9, 30)) is None  # gap between nudges


def test_unacknowledged_reminder_follow_up_has_buttons(store, settings):
    coach, sent = make_coach(store, settings)
    r = store.add_reminder("take out recycling", now_utc() - timedelta(hours=5))
    store.mark_fired(r.id, now_utc() - timedelta(hours=4), None)
    CheckinConfig("coach", "08:00", "13:00", "20:30", "", "").save(store)  # no quiet hours for this test
    assert asyncio.run(coach.send_nudge(now_utc(), CheckinConfig.load(store, settings))) == "followup"
    assert sent[-1][2] == r.id and "recycling" in sent[-1][1]
    assert asyncio.run(coach.send_nudge(now_utc(), CheckinConfig.load(store, settings))) is None


def test_stale_goal_check_in(store, settings):
    coach, sent = make_coach(store, settings)
    g = store.add_goal("Save $2000")
    store.add_goal_step(g.id, "open a savings account")
    later = now_utc() + timedelta(days=4)
    assert asyncio.run(coach.send_nudge(later, CheckinConfig.load(store, settings))) == "goal"
    assert "Save $2000" in sent[-1][1]
    assert asyncio.run(coach.send_nudge(later, CheckinConfig.load(store, settings))) is None


def test_no_nudges_while_user_is_chatting(store, settings):
    coach, sent = make_coach(store, settings)
    CheckinConfig("coach", "08:00", "13:00", "20:30", "", "").save(store)
    g = store.add_goal("x")
    store.add_goal_step(g.id, "y", now_utc() - timedelta(days=2))
    store.add_message("user", "hey", "web")
    assert tick(coach, now_utc() + timedelta(minutes=5)) != "followup"
    assert all(kind != "followup" for kind, _, _ in sent)


def test_daily_cap(store, settings):
    coach, sent = make_coach(store, settings)
    store.set_kv("checkin_state", {"day": at(settings, 1, 8).astimezone(settings.tz).date().isoformat(),
                                   "count": 6, "sent": {}, "last_nudge": None})
    assert tick(coach, at(settings, 1, 8, 0)) is None


def test_parse_day_keeps_today():
    from assistant.timeparse import format_day, parse_day

    tz = Settings().tz
    evening = datetime(2026, 9, 26, 18, 0, tzinfo=tz)
    assert format_day(parse_day("today", tz, evening), tz, evening) == "today"
    assert format_day(parse_day("tomorrow", tz, evening), tz, evening) == "tomorrow"
    assert format_day(parse_day("friday", tz, evening), tz, evening) == "Friday"
    assert format_day(parse_day("oct 10", tz, evening), tz, evening) == "Sat Oct 10"


def test_quiet_hours_wrap_midnight():
    tz = Settings().tz
    q = "22:00-07:30"
    assert in_quiet_hours(datetime(2026, 1, 1, 23, 0, tzinfo=tz), q)
    assert in_quiet_hours(datetime(2026, 1, 1, 7, 0, tzinfo=tz), q)
    assert not in_quiet_hours(datetime(2026, 1, 1, 7, 30, tzinfo=tz), q)
    assert not in_quiet_hours(datetime(2026, 1, 1, 12, 0, tzinfo=tz), "")


# --- goal tools -----------------------------------------------------------------

def test_goal_tools(store, settings):
    tb = Toolbox(store, settings, web=FakeWeb())

    def run(name, args):
        return asyncio.run(tb.run(name, args))

    out = run("create_goal", {
        "title": "Run a 5K", "target": "december 1", "why": "health",
        "steps": [{"text": "buy running shoes", "due": "saturday"}, {"text": "run 1 mile", "due": "not a date"},
                  "sign up for a race"],
    })
    assert "Saved goal #1" in out and "0/3 steps done" in out
    goal = store.get_goal(1)
    assert [s.text for s in goal.steps] == ["buy running shoes", "run 1 mile", "sign up for a race"]
    assert goal.steps[0].due_at is not None and goal.steps[1].due_at is None  # bad date doesn't sink the plan

    assert "Next step: #2 run 1 mile" in run("complete_goal_step", {"step_id": 1})
    assert "Added step" in run("add_goal_step", {"goal_id": 1, "text": "stretch", "due": "tomorrow"})
    assert "Progress logged" in run("update_goal", {"goal_id": 1, "progress_note": "ran 1.5 miles"})
    assert "complete" in run("update_goal", {"goal_id": "1", "status": "done"})
    assert store.list_goals() == []
    assert run("complete_goal_step", {"step_id": 99}).startswith("Error")
    assert run("create_goal", {"title": "Read more", "steps": "- read 10 pages\n- join a book club"}).startswith("Saved")
    assert "Weather for Boston" in run("get_weather", {})


def test_system_prompt_shows_goals_and_today(store, settings):
    g = store.add_goal("Run a 5K")
    store.add_goal_step(g.id, "buy shoes", now_utc() - timedelta(days=2))
    store.add_goal_step(g.id, "run a mile")
    prompt = Agent(settings, store, client=DownOllama()).system_prompt()
    assert "Goal #1 'Run a 5K': 0/2 steps done" in prompt
    assert "OVERDUE goal step #1: buy shoes" in prompt


def test_migration_adds_columns_to_old_db(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE reminders (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, due_at TEXT NOT NULL,"
                 " recurrence TEXT, status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, fired_at TEXT)")
    conn.commit()
    conn.close()
    store = Store(path)
    assert store.unacknowledged_reminders(now_utc()) == []


# --- web API ----------------------------------------------------------------

def test_goals_and_checkin_api(store, settings):
    c = TestClient(create_app(settings, store, Agent(settings, store, client=DownOllama()), Notifier()))
    c.post("/api/login", json={"password": "pw"})
    g = c.post("/api/goals", json={"title": "Learn Spanish", "target": "march 1"}).json()
    g = c.post(f"/api/goals/{g['id']}/steps", json={"text": "download an app", "due": "tomorrow"}).json()
    assert g["total"] == 1 and g["steps"][0]["due"] == "tomorrow"
    g = c.post(f"/api/steps/{g['steps'][0]['id']}/toggle").json()
    assert g["done"] == 1
    assert c.post(f"/api/goals/{g['id']}/status", json={"status": "done"}).json()["status"] == "done"
    assert c.post(f"/api/goals/{g['id']}/steps", json={"text": "x", "due": "blorp"}).status_code == 400

    cfg = c.get("/api/checkins").json()
    assert cfg["mode"] == "coach" and "balanced" in cfg["modes"]
    cfg.update(mode="light", morning="07:15")
    cfg.pop("modes")
    assert c.put("/api/checkins", json=cfg).json()["morning"] == "07:15"
    assert CheckinConfig.load(store, settings).mode == "light"
    cfg["evening"] = "8pm"
    assert c.put("/api/checkins", json=cfg).status_code == 400


# --- web tools parsing ------------------------------------------------------------

DDG_SAMPLE = """
<div class="result results_links results_links_deep web-result ">
  <div class="links_main links_deep result__body">
    <h2 class="result__title">
      <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.example.com%2Fhours&amp;rut=abc">Example Store &amp; Cafe - Hours</a>
    </h2>
    <a class="result__snippet" href="//duckduckgo.com/l/?uddg=x">Open <b>9am</b> to 9pm every day.</a>
  </div>
</div>
<div class="result results_links results_links_deep web-result result--ad">
  <a rel="nofollow" class="result__a" href="https://ads.example">Ad</a>
</div>
<div class="result results_links results_links_deep web-result ">
  <a rel="nofollow" class="result__a" href="https://second.example/page">Second</a>
  <a class="result__snippet" href="https://second.example/page">Another snippet</a>
</div>
"""


def test_parse_ddg_html():
    results = parse_ddg_html(DDG_SAMPLE)
    assert [r.url for r in results] == ["https://www.example.com/hours", "https://second.example/page"]
    assert results[0].title == "Example Store & Cafe - Hours"
    assert results[0].snippet == "Open 9am to 9pm every day."


def test_page_text_strips_chrome():
    page = "<html><head><title>x</title></head><body><nav>Home About Contact Menu Items Here</nav>" \
           "<script>var a = 'nope nope nope nope nope nope';</script>" \
           "<p>The store opens at nine in the morning on weekdays and ten on weekends.</p></body></html>"
    text = page_text(page)
    assert "opens at nine" in text and "nope" not in text and "About" not in text


def test_format_weather_and_pick_place():
    data = {
        "current": {"temperature_2m": 61.4, "apparent_temperature": 59.6, "weather_code": 2, "wind_speed_10m": 5.2},
        "daily": {"time": ["2026-09-26", "2026-09-27", "2026-09-28"], "weather_code": [61, 0, 3],
                  "temperature_2m_max": [66.2, 70, 68], "temperature_2m_min": [52, 55, 54],
                  "precipitation_probability_max": [80, 5, 10]},
    }
    out = format_weather(data, "Boston, Massachusetts")
    assert "Now: 61°F (feels 60°F), partly cloudy" in out
    assert "Today: light rain, high 66°F, low 52°F, 80% chance of rain" in out
    places = [{"name": "Portland", "admin1": "Oregon", "country": "United States"},
              {"name": "Portland", "admin1": "Maine", "country": "United States"}]
    assert pick_place(places, "Portland, ME")["admin1"] == "Maine"
    assert pick_place(places, "Portland")["admin1"] == "Oregon"


# --- Discord failures never take the service down ------------------------------

def test_bad_discord_token_disables_bot_without_hanging(tmp_path):
    import discord

    from assistant.db import Store as _Store
    from assistant.discord_bot import DiscordBot

    s = Settings(discord_token="bad", discord_owner_id=1, db_path=tmp_path / "d.db")
    st = _Store(s.db_path)
    bot = DiscordBot(s, st, Agent(s, st, client=DownOllama()))

    async def bad_start(token):
        raise discord.LoginFailure("Improper token has been passed.")

    bot.start = bad_start

    async def run():
        await bot.run_forever("bad")  # returns instead of raising
        r = st.add_reminder("x", now_utc())
        return await asyncio.wait_for(bot.send_reminder(r, False), 2)  # doesn't hang

    assert asyncio.run(run()) is False
    assert bot.disabled


def test_store_waits_for_locks(tmp_path):
    path = tmp_path / "l.db"
    Store(path)
    other = sqlite3.connect(path, check_same_thread=False)
    other.execute("BEGIN EXCLUSIVE")

    import threading
    t = threading.Timer(0.5, other.rollback)
    t.start()
    Store(path)  # would raise "database is locked" without the busy timeout
    t.join()
