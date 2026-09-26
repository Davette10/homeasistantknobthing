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


class Notifier:
    """Delivers a fired reminder to every registered sink (Discord DM, web push events)."""

    def __init__(self):
        self.sinks: List[Sink] = []
        self.web_queues: Set[asyncio.Queue] = set()

    def add_sink(self, sink: Sink) -> None:
        self.sinks.append(sink)

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
