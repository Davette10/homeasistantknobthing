"""Proactive check-ins: the assistant messages the user first.

Scheduled:  morning brief, midday nudge, evening check-in (at set times, once a day).
Nudges:     overdue goal steps, reminders that were never acknowledged, goals gone quiet.

Guard rails: quiet hours, a daily cap, a minimum gap between nudges, and no nudging
while the user is mid-conversation. Everything the coach sends is saved to chat
history, so when the user replies, the assistant knows what they're answering.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from typing import List, Optional, Tuple

from .agent import Agent
from .config import Settings
from .db import GoalStep, Store, now_utc
from .scheduler import Notifier
from .timeparse import format_day, format_local
from .webtools import Web, WebError

log = logging.getLogger(__name__)

TICK_SECONDS = 60
SCHEDULED_WINDOW = timedelta(hours=3)  # a morning brief is pointless after 11am
REMINDER_FOLLOWUP_AFTER = timedelta(hours=3)
STALE_GOAL_AFTER = timedelta(days=3)
RECENT_CHAT = timedelta(minutes=20)

MODES = {
    "off": set(),
    "light": {"morning", "followups"},
    "balanced": {"morning", "evening", "followups"},
    "coach": {"morning", "midday", "evening", "followups", "goal_checkins"},
}
DAILY_CAP = {"off": 0, "light": 3, "balanced": 4, "coach": 6}
NUDGE_GAP = {"off": timedelta(days=1), "light": timedelta(hours=3), "balanced": timedelta(hours=2),
             "coach": timedelta(minutes=75)}

_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


@dataclass
class CheckinConfig:
    mode: str
    morning: str
    midday: str
    evening: str
    quiet_hours: str
    weather_location: str

    @classmethod
    def load(cls, store: Store, settings: Settings) -> "CheckinConfig":
        cfg = cls(settings.coach_mode, settings.morning_time, settings.midday_time, settings.evening_time,
                  settings.quiet_hours, settings.weather_location)
        for k, v in (store.get_kv("checkin_config") or {}).items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
        if cfg.mode not in MODES:
            cfg.mode = "coach"
        return cfg

    def validate(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        for name in ("morning", "midday", "evening"):
            if not _HHMM.match(getattr(self, name)):
                raise ValueError(f"{name} time must look like 08:00")
        if self.quiet_hours and not all(_HHMM.match(p.strip()) for p in self.quiet_hours.split("-", 1)):
            raise ValueError("quiet hours must look like 22:00-07:30")

    def save(self, store: Store) -> None:
        self.validate()
        store.set_kv("checkin_config", asdict(self))

    @property
    def features(self) -> set:
        return MODES[self.mode]


def _hhmm(value: str) -> time:
    h, m = value.strip().split(":")
    return time(int(h), int(m))


def in_quiet_hours(local: datetime, quiet: str) -> bool:
    if not quiet or "-" not in quiet:
        return False
    start, end = (_hhmm(p) for p in quiet.split("-", 1))
    t = local.time()
    if start <= end:
        return start <= t < end
    return t >= start or t < end  # wraps past midnight


class Coach:
    def __init__(self, store: Store, settings: Settings, agent: Agent, notifier: Notifier, web: Optional[Web] = None):
        self.store = store
        self.settings = settings
        self.agent = agent
        self.notifier = notifier
        self.web = web or agent.toolbox.web
        self.tz = settings.tz

    async def run(self) -> None:
        log.info("check-in coach started")
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("coach tick failed")
            await asyncio.sleep(TICK_SECONDS)

    # --- bookkeeping -----------------------------------------------------------

    def _state(self, today: date) -> dict:
        state = self.store.get_kv("checkin_state") or {}
        if state.get("day") != today.isoformat():
            state = {"day": today.isoformat(), "count": 0, "sent": {}, "last_nudge": state.get("last_nudge")}
        return state

    def _user_recently_active(self, now: datetime) -> bool:
        recent = self.store.recent_messages(1)
        if not recent or recent[0]["role"] != "user":
            return False
        return now - datetime.fromisoformat(recent[0]["created_at"]) < RECENT_CHAT

    async def deliver(self, text: str, kind: str, reminder_id: Optional[int] = None) -> None:
        self.store.add_message("assistant", text, "checkin")
        await self.notifier.message(text, kind, reminder_id)

    # --- main loop -------------------------------------------------------------

    async def tick(self, now: Optional[datetime] = None) -> Optional[str]:
        """Send at most one proactive message. Returns its kind, or None."""
        now = now or now_utc()
        cfg = CheckinConfig.load(self.store, self.settings)
        if cfg.mode == "off":
            return None
        local = now.astimezone(self.tz)
        if in_quiet_hours(local, cfg.quiet_hours):
            return None
        state = self._state(local.date())
        if state["count"] >= DAILY_CAP[cfg.mode]:
            return None

        sent_kind = None
        for kind in ("morning", "midday", "evening"):
            if kind not in cfg.features or kind in state["sent"]:
                continue
            at = datetime.combine(local.date(), _hhmm(getattr(cfg, kind)), tzinfo=self.tz)
            if not at <= local < at + SCHEDULED_WINDOW:
                continue
            state["sent"][kind] = True  # even if there's nothing to say, don't retry all window
            text = await getattr(self, f"build_{kind}")(now, cfg)
            if text:
                await self.deliver(text, kind)
                sent_kind = kind
                break

        if sent_kind is None and "followups" in cfg.features and not self._user_recently_active(now):
            last = state.get("last_nudge")
            if last is None or now - datetime.fromisoformat(last) >= NUDGE_GAP[cfg.mode]:
                sent_kind = await self.send_nudge(now, cfg)
                if sent_kind:
                    state["last_nudge"] = now.isoformat()

        if sent_kind:
            state["count"] += 1
            log.info("sent %s check-in", sent_kind)
        self.store.set_kv("checkin_state", state)
        return sent_kind

    # --- facts ----------------------------------------------------------------

    def _open_steps(self, now: datetime) -> Tuple[List[Tuple[str, GoalStep]], List[Tuple[str, GoalStep]]]:
        """(due today, overdue) open steps of active goals, each paired with its goal's title.

        A step is overdue once its due *day* has passed - "due Saturday" means any time Saturday.
        """
        today = now.astimezone(self.tz).date()
        due_today, overdue = [], []
        for g in self.store.list_goals():
            for st in g.open_steps:
                if st.due_at is None:
                    continue
                d = st.due_at.astimezone(self.tz).date()
                if d == today:
                    due_today.append((g.title, st))
                elif d < today:
                    overdue.append((g.title, st))
        return due_today, overdue

    def _day_facts(self, now: datetime) -> List[str]:
        local = now.astimezone(self.tz)
        end = local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        facts = []
        for r in self.store.reminders_between(now, end):
            facts.append(f"Reminder at {format_local(r.due_at, self.tz, now).replace('today ', '')}: {r.text}")
        due_today, overdue = self._open_steps(now)
        for title, st in due_today:
            facts.append(f"Goal step due today ({title}): {st.text}")
        for title, st in overdue[:3]:
            facts.append(f"Overdue goal step ({title}): {st.text}, was due {format_day(st.due_at, self.tz, now)}")
        todos = self.store.list_todos("to-do")
        if todos:
            facts.append("To-do list: " + "; ".join(t.text for t in todos[:5]) + (f" (+{len(todos) - 5} more)" if len(todos) > 5 else ""))
        return facts

    async def _weather_line(self, cfg: CheckinConfig) -> Optional[str]:
        if not cfg.weather_location:
            return None
        try:
            report = await self.web.weather(cfg.weather_location)
        except (WebError, ValueError, KeyError, TypeError) as e:
            log.info("no weather for brief: %s", e)
            return None
        lines = report.splitlines()
        return " / ".join(lines[1:3])  # "Now: ..." and "Today: ..."

    # --- scheduled messages -------------------------------------------------------

    async def build_morning(self, now: datetime, cfg: CheckinConfig) -> Optional[str]:
        facts = self._day_facts(now)
        weather = await self._weather_line(cfg)
        goals = self.store.list_goals()
        fact_text = "\n".join(f"- {f}" for f in facts) or "- Nothing scheduled."
        instruction = (
            "Write the user's morning brief. Open with a short, warm good-morning line"
            + (" that mentions the weather" if weather else "")
            + ". Then list today's plan as short bullets using ONLY the facts below - don't invent anything. "
            "End with one question that helps them focus on the most important thing today"
            + (" or makes progress on a goal" if goals else "")
            + ". Under 120 words.\n"
            + (f"Weather: {weather}\n" if weather else "")
            + f"Today's facts:\n{fact_text}"
        )
        fallback = "Good morning! ☀️" + (f" {weather}." if weather else "") + "\n\n"
        fallback += "\n".join(f"• {f}" for f in facts) if facts else "Nothing on the calendar today."
        fallback += "\n\nWhat's the one thing you want to get done today?"
        return await self.agent.compose(instruction, fallback)

    async def build_midday(self, now: datetime, cfg: CheckinConfig) -> Optional[str]:
        due_today, overdue = self._open_steps(now)
        items = [f"goal step for '{t}': {st.text}" for t, st in due_today + overdue]
        items += [f"to-do: {t.text}" for t in self.store.list_todos("to-do")[:3]]
        if not items:
            return None
        instruction = (
            "Send a short midday nudge (1-3 sentences) about the most important open item below. "
            "Ask how it's going or offer to help break it down. Friendly, not naggy.\n"
            "Open items:\n" + "\n".join(f"- {i}" for i in items[:5])
        )
        return await self.agent.compose(instruction, f"Quick midday check: how's it going with {items[0].split(': ', 1)[1]}? 💪")

    async def build_evening(self, now: datetime, cfg: CheckinConfig) -> Optional[str]:
        due_today, overdue = self._open_steps(now)
        items = [f"goal step for '{t}': {st.text}" for t, st in due_today + overdue]
        items += [f"to-do: {t.text}" for t in self.store.list_todos("to-do")[:3]]
        if items:
            instruction = (
                "Send a short evening check-in (2-4 sentences). Ask how the day went with the specific items below "
                "and invite them to tell you what got done so you can check it off. Warm and encouraging.\n"
                "Items:\n" + "\n".join(f"- {i}" for i in items[:5])
            )
            fallback = "Evening check-in 🌙 How did today go? Did you get to: " + "; ".join(
                i.split(": ", 1)[1] for i in items[:3]) + "? Tell me what's done and I'll check it off."
        else:
            instruction = (
                "Send a short, friendly evening check-in (1-2 sentences): ask how their day was and whether "
                "there's anything for tomorrow you should remind them about or plan."
            )
            fallback = "Evening check-in 🌙 How was your day? Anything for tomorrow I should remind you about?"
        return await self.agent.compose(instruction, fallback)

    # --- nudges ----------------------------------------------------------------

    async def send_nudge(self, now: datetime, cfg: CheckinConfig) -> Optional[str]:
        # 1. A goal step whose day has passed.
        _, overdue = self._open_steps(now)
        for title, st in overdue:
            if st.nudged_at is None:
                self.store.mark_step_nudged(st.id, now)
                instruction = (
                    f"Follow up on a goal step that was due {format_day(st.due_at, self.tz, now)} and isn't checked off: "
                    f"'{st.text}' (goal: {title}). In 1-3 sentences, ask if they did it; if not, help them pick a "
                    "new time or a smaller first step. Supportive, no guilt."
                )
                await self.deliver(await self.agent.compose(
                    instruction, f"Hey, did you get to '{st.text}' for {title}? If not, want to pick a new day for it?"
                ), "followup")
                return "followup"

        # 2. A reminder that went off and was never marked done or snoozed.
        for r in self.store.unacknowledged_reminders(now - REMINDER_FOLLOWUP_AFTER):
            self.store.mark_followed_up(r.id)
            if now - (r.fired_at or now) > timedelta(hours=24):
                continue  # too old to be worth asking about
            text = f"Did you get to this? 👀\n**{r.text}**"
            await self.deliver(text, "followup", reminder_id=r.id)
            return "followup"

        # 3. A goal that's gone quiet.
        if "goal_checkins" in cfg.features:
            for g in self.store.list_goals():
                quiet_since = max(g.last_activity_at, g.checked_in_at or g.last_activity_at)
                if now - quiet_since >= STALE_GOAL_AFTER:
                    self.store.mark_goal_checked_in(g.id, now)
                    nxt = g.open_steps[0].text if g.open_steps else None
                    instruction = (
                        f"Their goal '{g.title}' has had no progress for a few days "
                        f"({g.done_count}/{len(g.steps)} steps done"
                        + (f", next step: '{nxt}'" if nxt else "") + "). "
                        "Check in with 1-3 sentences: ask how it's going and suggest one tiny action for today. "
                        "If it no longer matters to them, that's okay too."
                    )
                    fallback = f"How's '{g.title}' going? " + (f"Could you knock out '{nxt}' today?" if nxt else "Want to plan the next step?")
                    await self.deliver(await self.agent.compose(instruction, fallback), "goal")
                    return "goal"
        return None
