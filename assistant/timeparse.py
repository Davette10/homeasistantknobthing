"""Turn "in 20 minutes" / "tomorrow at 5pm" / "2026-10-01 14:30" into datetimes,
and work out the next time a repeating reminder should fire.

Small local models are bad at date math, so the LLM hands us the time the way the
user said it and we do the arithmetic here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

import dateparser
from dateutil import rrule

UTC = timezone.utc
DEFAULT_HOUR = 9  # "remind me monday" -> Monday 9am

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# Phrases dateparser doesn't know, rewritten to ones it does.
_PHRASES = [
    (r"\bmidnight\b", "12am"),
    (r"\bnoon\b", "12pm"),
    (r"\bend of (?:the )?day\b|\beod\b", "today 5pm"),
    (r"\bin an hour\b", "in 1 hour"),
    (r"\bin a minute\b", "in 1 minute"),
    (r"\bin (?:a )?half (?:an )?hour\b", "in 30 minutes"),
]
# Part-of-day phrase -> (day word, default time if no clock given, am/pm for a bare hour)
_PARTS_OF_DAY = [
    (r"\btomorrow morning\b", "tomorrow", "9am", "am"),
    (r"\btomorrow afternoon\b", "tomorrow", "3pm", "pm"),
    (r"\btomorrow evening\b", "tomorrow", "6pm", "pm"),
    (r"\btomorrow night\b", "tomorrow", "8pm", "pm"),
    (r"\bthis morning\b", "today", "9am", "am"),
    (r"\bthis afternoon\b", "today", "3pm", "pm"),
    (r"\bthis evening\b", "today", "6pm", "pm"),
    (r"\btonight\b", "today", "8pm", "pm"),
    (r"\bin the morning\b", "", "9am", "am"),
    (r"\bin the afternoon\b", "", "3pm", "pm"),
    (r"\bin the evening\b", "", "6pm", "pm"),
    (r"\bat night\b", "", "8pm", "pm"),
]
# A clock time: "5pm", "5:30", "5:30 pm", or "at 5".
_CLOCK = re.compile(
    r"\b(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>[ap])\.?m\b\.?"
    r"|\b(?P<h2>\d{1,2}):(?P<m2>\d{2})\b"
    r"|\bat\s+(?P<h3>\d{1,2})\b(?![:/\-]|\s*(?:st|nd|rd|th)\b)"
)
_HAS_DAY = re.compile(
    r"\b(?:today|tomorrow|mon|tue|wed|thu|fri|sat|sun|next|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
    r"|\d{4}-\d|\d{1,2}/\d{1,2}"
)
_HAS_CLOCK = re.compile(r"\d\s*(?:am|pm)\b|\d:\d\d|\bnoon\b|\bmidnight\b|\bin \d|\bin an?\b", re.I)


class TimeParseError(ValueError):
    pass


def _normalize(text: str) -> Tuple[List[str], bool]:
    """Rewrite `text` into forms dateparser handles.

    Returns candidate strings (tried in order) and whether the time is a bare
    hour like "at 8" with no am/pm and no day, which we resolve to the soonest one.
    """
    t = " " + text.strip().lower().rstrip(".!?") + " "
    for pattern, repl in _PHRASES:
        t = re.sub(pattern, repl, t)

    period = None
    for pattern, day, default, ampm in _PARTS_OF_DAY:
        if re.search(pattern, t):
            has_clock = _CLOCK.search(re.sub(pattern, " ", t))
            t = re.sub(pattern, f" {day} " if has_clock else f" {day} {default} ", t)
            period = ampm if has_clock else None
            break

    bare = False
    m = _CLOCK.search(t)
    if m:
        hour = int(m.group("h") or m.group("h2") or m.group("h3"))
        minute = int(m.group("m") or m.group("m2") or 0)
        ampm = (m.group("ap") + "m") if m.group("ap") else None
        if ampm is None and period and 1 <= hour <= 12:
            ampm = period
        if ampm is None and 1 <= hour <= 11 and not _HAS_DAY.search(t):
            bare = True
        if ampm is None and 1 <= hour <= 11:
            # Nobody wants a 4am reminder: 1-6 means afternoon, 7-11 morning.
            ampm = "pm" if hour <= 6 else "am"
        clock = f"{hour}:{minute:02d}{ampm or ''}"
        t = f"{t[:m.start()]} {clock} {t[m.end():]}"

    t = re.sub(r"\s+", " ", t).strip()
    # dateparser chokes on "next monday", "this friday", "on friday", "at 5pm"
    loose = re.sub(r"\s+", " ", re.sub(r"\b(?:next|this|on|at|coming|by)\b", " ", t)).strip()
    return list(dict.fromkeys([t, loose])), bare


def parse_when(text: str, tz: ZoneInfo, now: Optional[datetime] = None) -> datetime:
    """Parse a user-style time into an aware UTC datetime in the future."""
    if not text or not str(text).strip():
        raise TimeParseError("no time given")
    text = str(text).strip()
    now = (now or datetime.now(UTC)).astimezone(tz)

    dt: Optional[datetime] = None
    bare = False
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
    except ValueError:
        settings = {
            "TIMEZONE": tz.key,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": now.replace(tzinfo=None),
        }
        candidates, bare = _normalize(text)
        for candidate in candidates:
            dt = dateparser.parse(candidate, settings=settings)
            if dt is not None:
                break
        if dt is None:
            raise TimeParseError(f"couldn't understand the time {text!r}")
        # "monday" / "tomorrow" with no clock time come back as midnight or the
        # current time; a morning reminder is saner.
        local = dt.astimezone(tz)
        if not _HAS_CLOCK.search(text) and (local.hour, local.minute) in ((0, 0), (now.hour, now.minute)):
            dt = local.replace(hour=DEFAULT_HOUR, minute=0, second=0)

    dt = dt.astimezone(tz)
    if bare:
        # "at 8" said at 3pm means 8pm today, not 8am tomorrow: take the soonest match.
        options = [dt + timedelta(hours=12 * k) for k in range(-2, 4)]
        options = [c for c in options if c > now]
        sane = [c for c in options if not 0 < c.hour < 7]  # skip 1am-6am
        dt = min(sane or options or [dt])
    if dt <= now:
        # "5pm" said at 6pm means tomorrow at 5pm.
        if now - dt < timedelta(days=1):
            dt = _add_days_local(dt, 1, tz)
        else:
            raise TimeParseError(f"{text!r} is in the past")
    return dt.astimezone(UTC).replace(microsecond=0)


def parse_day(text: str, tz: ZoneInfo, now: Optional[datetime] = None) -> datetime:
    """Day-level date for goal steps and targets: "today" means today even at 5pm.

    Returns 9am local on that day, as UTC.
    """
    local = (now or datetime.now(UTC)).astimezone(tz)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    dt = parse_when(text, tz, now=midnight).astimezone(tz)
    return dt.replace(hour=DEFAULT_HOUR, minute=0, second=0).astimezone(UTC)


def format_day(dt_utc: datetime, tz: ZoneInfo, now: Optional[datetime] = None) -> str:
    """'today', 'tomorrow', 'Friday', 'Sat Oct 10' - no clock time."""
    local = dt_utc.astimezone(tz).date()
    today = (now or datetime.now(UTC)).astimezone(tz).date()
    delta = (local - today).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta == -1:
        return "yesterday"
    if 1 < delta < 7:
        return dt_utc.astimezone(tz).strftime("%A")
    fmt = "%a %b %d" if local.year == today.year else "%a %b %d %Y"
    return dt_utc.astimezone(tz).strftime(fmt).replace(" 0", " ")


def _add_days_local(dt: datetime, days: int, tz: ZoneInfo) -> datetime:
    """Add days keeping the same wall-clock time across DST changes."""
    naive = dt.astimezone(tz).replace(tzinfo=None) + timedelta(days=days)
    return naive.replace(tzinfo=tz)


# --- Repeating reminders ----------------------------------------------------

_FREQS = {
    "hourly": rrule.HOURLY,
    "daily": rrule.DAILY,
    "weekly": rrule.WEEKLY,
    "monthly": rrule.MONTHLY,
    "yearly": rrule.YEARLY,
}
_RRULE_DAYS = [rrule.MO, rrule.TU, rrule.WE, rrule.TH, rrule.FR, rrule.SA, rrule.SU]


@dataclass
class Recurrence:
    freq: str  # hourly | daily | weekly | monthly | yearly
    interval: int = 1
    weekdays: List[int] = field(default_factory=list)  # 0=Mon .. 6=Sun

    def to_dict(self) -> dict:
        return {"freq": self.freq, "interval": self.interval, "weekdays": self.weekdays}

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> Optional["Recurrence"]:
        if not d:
            return None
        return cls(freq=d["freq"], interval=int(d.get("interval") or 1), weekdays=list(d.get("weekdays") or []))

    def describe(self) -> str:
        if self.freq == "weekly" and self.weekdays:
            if sorted(self.weekdays) == [0, 1, 2, 3, 4]:
                base = "every weekday"
            elif sorted(self.weekdays) == [5, 6]:
                base = "every weekend day"
            else:
                names = ", ".join(WEEKDAYS[d].capitalize() for d in sorted(self.weekdays))
                base = f"every {names}"
            return base if self.interval == 1 else f"{base} (every {self.interval} weeks)"
        unit = {"hourly": "hour", "daily": "day", "weekly": "week", "monthly": "month", "yearly": "year"}[self.freq]
        return f"every {unit}" if self.interval == 1 else f"every {self.interval} {unit}s"

    def _rule(self, start_local: datetime) -> rrule.rrule:
        kwargs = {"interval": max(1, self.interval), "dtstart": start_local}
        if self.weekdays:
            kwargs["byweekday"] = [_RRULE_DAYS[d] for d in self.weekdays]
        return rrule.rrule(_FREQS[self.freq], **kwargs)

    def first(self, start_utc: datetime, tz: ZoneInfo) -> datetime:
        """First occurrence at or after `start_utc` (e.g. "every weekday 8am" asked on a Saturday)."""
        start_local = start_utc.astimezone(tz).replace(tzinfo=None)
        occ = self._rule(start_local).after(start_local, inc=True)
        return occ.replace(tzinfo=tz).astimezone(UTC)

    def next_after(self, prev_due_utc: datetime, after_utc: datetime, tz: ZoneInfo) -> datetime:
        """Next occurrence strictly after `after_utc`, anchored on the previous due time.

        Works in local wall-clock time so "daily at 8am" stays at 8am across DST.
        Missed occurrences (device was off) are skipped rather than replayed.
        """
        start_local = prev_due_utc.astimezone(tz).replace(tzinfo=None)
        after_local = after_utc.astimezone(tz).replace(tzinfo=None)
        occ = self._rule(start_local).after(after_local)
        return occ.replace(tzinfo=tz).astimezone(UTC)


def parse_recurrence(repeat: Optional[str], every: Optional[int] = None, days=None) -> Optional[Recurrence]:
    """Build a Recurrence from loose tool arguments. Returns None for one-off reminders."""
    r = (repeat or "").strip().lower().replace("-", " ").replace("_", " ")
    if r.startswith("every "):
        r = r[len("every "):]
    if r in ("", "none", "no", "never", "once", "one time", "false", "null"):
        if not days:
            return None
        r = "weekly"

    weekdays = _parse_days(days)
    interval = 1
    try:
        interval = max(1, int(every)) if every not in (None, "") else 1
    except (TypeError, ValueError):
        interval = 1

    aliases = {
        "hour": "hourly", "day": "daily", "week": "weekly", "month": "monthly", "year": "yearly",
        "annually": "yearly", "annual": "yearly", "everyday": "daily", "nightly": "daily",
        "biweekly": "weekly",
    }
    if r == "biweekly" and interval == 1:
        interval = 2
    if r in ("weekday", "weekdays", "workday", "workdays"):
        return Recurrence("weekly", interval, [0, 1, 2, 3, 4])
    if r in ("weekend", "weekends"):
        return Recurrence("weekly", interval, [5, 6])
    day_list = _parse_days(r)
    if day_list:
        return Recurrence("weekly", interval, day_list)
    freq = aliases.get(r, r)
    if freq not in _FREQS:
        raise TimeParseError(f"unknown repeat {repeat!r}; use daily, weekdays, weekends, weekly, monthly, yearly or hourly")
    return Recurrence(freq, interval, weekdays if freq == "weekly" else [])


def _parse_days(days) -> List[int]:
    if not days:
        return []
    if isinstance(days, str):
        days = re.split(r"[,\s/&]+|\band\b", days)
    out = set()
    for d in days:
        if isinstance(d, int) and 0 <= d <= 6:
            out.add(d)
            continue
        key = str(d).strip().lower()[:3]
        if key in WEEKDAYS:
            out.add(WEEKDAYS.index(key))
    return sorted(out)


def format_local(dt_utc: datetime, tz: ZoneInfo, now: Optional[datetime] = None) -> str:
    """Friendly local time: 'today 5:00 PM', 'tomorrow 9:00 AM', 'Fri Oct 2, 3:00 PM'."""
    local = dt_utc.astimezone(tz)
    today = (now or datetime.now(UTC)).astimezone(tz).date()
    clock = local.strftime("%I:%M %p").lstrip("0")
    if local.date() == today:
        return f"today {clock}"
    if local.date() == today + timedelta(days=1):
        return f"tomorrow {clock}"
    if 0 < (local.date() - today).days < 7:
        return f"{local.strftime('%A')} {clock}"
    fmt = "%a %b %d" if local.year == today.year else "%a %b %d %Y"
    return f"{local.strftime(fmt).replace(' 0', ' ')}, {clock}"
