"""Discord bot: chats with the owner in DMs and delivers reminders with Done/Snooze buttons.

It ignores everyone except DISCORD_OWNER_ID.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import List

import discord

from .agent import Agent
from .config import Settings
from .db import Reminder, Store, now_utc
from .timeparse import format_local

log = logging.getLogger(__name__)

DISCORD_LIMIT = 2000


def split_message(text: str, limit: int = DISCORD_LIMIT) -> List[str]:
    """Split on paragraph/line boundaries to fit Discord's 2000-char limit."""
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current.strip():
        chunks.append(current)
    return chunks or [text[:limit]]


def reminder_view(rid: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label="Done", style=discord.ButtonStyle.success, custom_id=f"rem:done:{rid}"))
    view.add_item(discord.ui.Button(label="10 min", emoji="💤", custom_id=f"rem:snooze:{rid}:10"))
    view.add_item(discord.ui.Button(label="1 hour", emoji="💤", custom_id=f"rem:snooze:{rid}:60"))
    return view


class DiscordBot(discord.Client):
    def __init__(self, settings: Settings, store: Store, agent: Agent):
        intents = discord.Intents.default()
        intents.dm_messages = True
        super().__init__(intents=intents)
        self.settings = settings
        self.store = store
        self.agent = agent

    async def on_ready(self) -> None:
        log.info("Discord bot logged in as %s", self.user)

    async def _owner(self) -> discord.User:
        return self.get_user(self.settings.discord_owner_id) or await self.fetch_user(self.settings.discord_owner_id)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not isinstance(message.channel, discord.DMChannel):
            return
        if message.author.id != self.settings.discord_owner_id:
            return
        text = message.content.strip()
        if not text:
            return
        async with message.channel.typing():
            reply = await self.agent.reply(text, source="discord")
        for chunk in split_message(reply):
            await message.channel.send(chunk)

    async def send_reminder(self, r: Reminder, late: bool) -> bool:
        if not self.is_ready():
            await self.wait_until_ready()
        owner = await self._owner()
        header = "⏰ **Reminder**" + (" *(late - I was offline)*" if late else "")
        body = f"{header}\n{r.text}"
        if r.recurrence:
            # r.due_at is the time that just fired; the store already holds the next one.
            fresh = self.store.get_reminder(r.id)
            nxt = format_local(fresh.due_at, self.settings.tz) if fresh else "?"
            body += f"\n-# repeats {r.recurrence.describe()} · next {nxt}"
        await owner.send(body, view=reminder_view(r.id))
        return True

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        cid = (interaction.data or {}).get("custom_id", "") if interaction.data else ""
        if not cid.startswith("rem:"):
            return
        if interaction.user.id != self.settings.discord_owner_id:
            await interaction.response.send_message("Not your reminder.", ephemeral=True)
            return
        parts = cid.split(":")
        action, rid = parts[1], int(parts[2])
        r = self.store.get_reminder(rid)
        text = r.text if r else "reminder"
        if action == "done":
            self.store.complete_reminder(rid)
            await interaction.response.edit_message(content=f"✅ ~~{text}~~", view=None)
        elif action == "snooze":
            minutes = int(parts[3])
            until = now_utc() + timedelta(minutes=minutes)
            self.store.snooze_reminder(rid, until)
            label = f"{minutes} min" if minutes < 60 else f"{minutes // 60} hour"
            await interaction.response.edit_message(
                content=f"💤 **{text}**\n-# snoozed {label}, back at {format_local(until, self.settings.tz)}", view=None
            )
