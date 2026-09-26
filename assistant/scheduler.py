"""Fires due reminders and fans them out to Discord and any open web UI tabs."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Awaitable, Callable, List, Optional, Set

from .config import Settings
from .db import Reminder, Store, now_utc

log = logging.getLogger(__name__)

TICK_SECONDS = 10
LATE_AFTER = timedelta(minutes=5)

Sink = Callable[[Reminder, bool], Awaitable[bool]]
MessageSink = Callable[[str, str, Optional[int]], Awaitable[None]]


class Notifier:
    """Delivers reminders and proactive messages to Discord and any open web UI tabs."""

    def __init__(self):
        self.sinks: List[Sink] = []
        self.message_sinks: List[MessageSink] = []
        self.web_queues: Set[asyncio.Queue] = set()

    def add_sink(self, sink: Sink) -> None:
        self.sinks.append(sink)

    def add_message_sink(self, sink: MessageSink) -> None:
        self.message_sinks.append(sink)

    async def message(self, text: str, kind: str, reminder_id: Optional[int] = None) -> None:
        """A message the assistant starts on its own (check-in, brief, follow-up)."""
        self.publish_web({"type": "message", "text": text, "kind": kind, "reminder_id": reminder_id})
        for sink in self.message_sinks:
            try:
                await sink(text, kind, reminder_id)
            except Exception:
                log.exception("message sink failed (%s)", kind)

    def subscribe_web(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self.web_queues.add(q)
        return q

    def unsubscribe_web(self, q: asyncio.Queue) -> None:
        self.web_queues.discard(q)

    def publish_web(self, event: dict) -> None:
        for q in list(self.web_queues):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def reminder(self, r: Reminder, late: bool, when_text: str) -> None:
        self.publish_web({"type": "reminder", "id": r.id, "text": r.text, "late": late, "when": when_text})
        for sink in self.sinks:
            try:
                await sink(r, late)
            except Exception:
                log.exception("reminder sink failed for #%s", r.id)


class Scheduler:
    def __init__(self, store: Store, settings: Settings, notifier: Notifier):
        self.store = store
        self.settings = settings
        self.notifier = notifier

    async def run(self) -> None:
        log.info("reminder scheduler started")
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            await asyncio.sleep(TICK_SECONDS)

    async def tick(self, now: Optional[datetime] = None) -> List[Reminder]:
        now = now or now_utc()
        fired = []
        for r in self.store.due_reminders(now):
            late = now - r.due_at > LATE_AFTER
            next_due = r.recurrence.next_after(r.due_at, now, self.settings.tz) if r.recurrence else None
            # Mark first so a slow/failed delivery can't cause a double send.
            self.store.mark_fired(r.id, now, next_due)
            when_text = r.due_at.astimezone(self.settings.tz).strftime("%I:%M %p").lstrip("0")
            log.info("firing reminder #%s%s: %s", r.id, " (late)" if late else "", r.text)
            await self.notifier.reminder(r, late, when_text)
            fired.append(r)
        return fired
