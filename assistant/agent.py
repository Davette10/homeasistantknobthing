"""The agent loop: build the prompt, stream from Ollama, run tool calls, repeat."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import AsyncIterator, Dict, List, Optional

import httpx

from .config import Settings
from .db import Store
from .timeparse import format_day, format_local
from .tools import TOOL_LABELS, TOOL_SPECS, Toolbox

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 5
MAX_MEMORIES_IN_PROMPT = 80


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._http = httpx.AsyncClient(base_url=settings.ollama_url, timeout=httpx.Timeout(600, connect=10))

    async def stream_chat(self, messages: List[dict], tools: Optional[List[dict]] = None) -> AsyncIterator[dict]:
        """Yield Ollama's streamed chunks ({"message": {...}, "done": bool})."""
        body = {
            "model": self.settings.model,
            "messages": messages,
            "stream": True,
            "think": self.settings.think,
            "keep_alive": -1,  # keep the model loaded so replies start fast
            "options": {"num_ctx": self.settings.num_ctx, "temperature": 0.6},
        }
        if tools:
            body["tools"] = tools
        try:
            async with self._http.stream("POST", "/api/chat", json=body) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread()).decode(errors="replace")[:300]
                    raise OllamaError(f"Ollama returned {resp.status_code}: {detail}")
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    if chunk.get("error"):
                        raise OllamaError(chunk["error"])
                    yield chunk
        except httpx.ConnectError as e:
            raise OllamaError(f"can't reach Ollama at {self.settings.ollama_url} - is it running?") from e

    async def health(self) -> Dict[str, object]:
        """Check Ollama is up and the configured model is pulled."""
        try:
            r = await self._http.get("/api/tags")
            r.raise_for_status()
        except httpx.HTTPError as e:
            return {"ok": False, "error": f"can't reach Ollama at {self.settings.ollama_url}: {e}"}
        names = [m.get("name", "") for m in r.json().get("models", [])]
        want = self.settings.model if ":" in self.settings.model else self.settings.model + ":latest"
        if want not in names:
            return {"ok": False, "error": f"model {self.settings.model} is not pulled (run: ollama pull {self.settings.model})"}
        return {"ok": True, "model": self.settings.model}

    async def close(self) -> None:
        await self._http.aclose()


class ThinkFilter:
    """Strips <think>...</think> blocks from streamed text, in case a model emits them anyway."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.buf = ""
        self.inside = False

    def feed(self, text: str) -> str:
        self.buf += text
        out = []
        while True:
            tag = self.CLOSE if self.inside else self.OPEN
            idx = self.buf.find(tag)
            if idx >= 0:
                if not self.inside:
                    out.append(self.buf[:idx])
                self.buf = self.buf[idx + len(tag):]
                self.inside = not self.inside
                continue
            # Hold back anything that could be the start of a tag.
            keep = 0
            for n in range(1, len(tag)):
                if self.buf.endswith(tag[:n]):
                    keep = n
            emit, self.buf = self.buf[: len(self.buf) - keep], self.buf[len(self.buf) - keep:]
            if not self.inside:
                out.append(emit)
            return "".join(out)

    def flush(self) -> str:
        rest, self.buf = ("" if self.inside else self.buf), ""
        return rest


class Agent:
    def __init__(self, settings: Settings, store: Store, client: Optional[OllamaClient] = None):
        self.settings = settings
        self.store = store
        self.client = client or OllamaClient(settings)
        self.toolbox = Toolbox(store, settings)
        # One conversation at a time: the Jetson can only run one generation anyway,
        # and it keeps web + Discord messages from interleaving in the history.
        self._lock = asyncio.Lock()

    def system_prompt(self, now: Optional[datetime] = None) -> str:
        s = self.settings
        now = (now or datetime.now(s.tz)).astimezone(s.tz)
        who = s.user_name or "the user"
        parts = [
            f"You are {s.assistant_name}, a personal AI agent for {who}. You run privately on their own computer "
            "at home and talk with them through a web app and Discord. You don't just answer questions - you help "
            "them stay on top of things, take tasks off their plate, and turn goals into action.",
            s.persona(),
            f"Right now it is {now.strftime('%A, %B %d, %Y, %I:%M %p').replace(' 0', ' ')} ({s.timezone}).",
            "What you can do:\n"
            "- Reminders (set_reminder, list_reminders, cancel_reminder). They get pushed to the user's phone.\n"
            "- To-do and shopping lists (add_todo, list_todos, complete_todo, delete_todo).\n"
            "- Goals with action plans (create_goal, add_goal_step, complete_goal_step, update_goal).\n"
            "- Look things up online (web_search) and check the weather (get_weather).\n"
            "- Remember lasting facts about the user (remember, forget). When they mention something worth "
            "knowing later - people in their life, birthdays, preferences, routines, projects - save it without "
            "being asked. Don't save small talk.\n"
            "- Brainstorm and suggest ideas: gifts, projects, meals, plans, things to do. Give a few concrete, "
            "specific options tailored to what you know about them, not generic lists.",
            "Being their coach:\n"
            "- When they mention something they want to achieve, offer to make it a goal. Ask one or two quick "
            "questions if needed (deadline, where they're starting from), then propose 3-7 small, concrete, dated "
            "steps. Once they're happy, save it with create_goal.\n"
            "- When they say they did something, check it off (complete_goal_step / complete_todo) and celebrate "
            "briefly. When they slip, be kind but steer them to the very next small step.\n"
            "- You check in with them on your own (morning brief, midday nudge, evening check-in). If they're "
            "answering one of your check-ins, update goals and lists from what they tell you.\n"
            "- Offer a reminder when a plan needs one.",
            "Rules:\n"
            "- Actually call the tools. Never say something is saved unless the tool result confirms it.\n"
            "- For times, pass them the way the user said them ('in 20 minutes', 'tomorrow at 9am', 'friday 5pm') "
            "or as 'YYYY-MM-DD HH:MM'. If they didn't say when for a reminder, ask.\n"
            "- If a tool returns an error, fix the call or tell the user; don't pretend it worked.\n"
            "- Use web_search for anything current or that you're unsure about, and mention where info came from.\n"
            "- To cancel or check off a reminder or list item, look up its id first with list_reminders/list_todos.\n"
            "- Keep replies short - this is a chat, usually on a phone. Use a list only when it helps.",
        ]
        goals = self.goals_summary()
        if goals:
            parts.append(goals)
        today = self.today_summary(now)
        if today:
            parts.append(today)
        memories = self.store.list_memories(MAX_MEMORIES_IN_PROMPT)
        if memories:
            facts = "\n".join(f"- [#{m.id}] {m.fact}" for m in memories)
            parts.append(f"What you know about {who} (ids are for the forget tool):\n{facts}")
        else:
            parts.append(f"You don't know anything about {who} yet. Learn as you go.")
        return "\n\n".join(p for p in parts if p)

    def goals_summary(self, max_steps: int = 3) -> str:
        goals = self.store.list_goals()
        if not goals:
            return ""
        tz = self.settings.tz
        lines = ["Their active goals (ids are for the goal tools):"]
        for g in goals[:6]:
            head = f"- Goal #{g.id} {g.title!r}: {g.done_count}/{len(g.steps)} steps done"
            if g.target_at:
                head += f", target {format_day(g.target_at, tz)}"
            lines.append(head)
            for st in g.open_steps[:max_steps]:
                due = f" (due {format_day(st.due_at, tz)})" if st.due_at else ""
                lines.append(f"    next: step #{st.id} {st.text}{due}")
            if g.notes:
                lines.append(f"    latest progress: {g.notes.splitlines()[-1]}")
        return "\n".join(lines)

    def today_summary(self, now: datetime) -> str:
        """Reminders left today and overdue goal steps, so 'what's my day?' needs no tool calls."""
        tz = self.settings.tz
        start = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        lines = []
        for r in self.store.reminders_between(now, end)[:8]:
            lines.append(f"- {format_local(r.due_at, tz, now)}: reminder #{r.id} {r.text}")
        for g in self.store.list_goals():
            for st in g.open_steps:
                if st.due_at and start <= st.due_at < end:
                    lines.append(f"- goal step #{st.id} due today: {st.text}")
        # A step is overdue once its due day has passed ("due Saturday" = any time Saturday).
        for st in self.store.overdue_steps(start)[:5]:
            lines.append(f"- OVERDUE goal step #{st.id}: {st.text} (was due {format_day(st.due_at, tz, now)})")
        return ("Still coming up today / needs attention:\n" + "\n".join(lines)) if lines else ""

    async def compose(self, instruction: str, fallback: str) -> str:
        """Write a proactive message (check-in, brief) in the assistant's voice. No tools.

        Waits for any in-progress chat to finish, and falls back to `fallback` if the model is down.
        """
        async with self._lock:
            messages = [
                {"role": "system", "content": self.system_prompt()},
                *self._history()[-6:],
                {"role": "user", "content": "[Automatic check-in - the user did not send this message.]\n" + instruction},
            ]
            think = ThinkFilter()
            out = []
            try:
                async for chunk in self.client.stream_chat(messages, None):
                    text = (chunk.get("message") or {}).get("content")
                    if text:
                        out.append(think.feed(text))
                out.append(think.flush())
            except OllamaError as e:
                log.warning("check-in compose failed, using fallback: %s", e)
                return fallback
            text = "".join(out).strip()
            return text or fallback

    def _history(self) -> List[dict]:
        return [
            {"role": m["role"], "content": m["content"]}
            for m in self.store.recent_messages(self.settings.history_messages)
        ]

    async def chat(self, text: str, source: str, save: bool = True) -> AsyncIterator[dict]:
        """Run one user turn. Yields events:
        {"type": "token", "text"} streamed reply text
        {"type": "tool", "name", "label", "result"} after each tool call
        {"type": "done", "text"} the full final reply
        {"type": "error", "text"} if the model couldn't be reached
        """
        async with self._lock:
            messages = [{"role": "system", "content": self.system_prompt()}, *self._history()]
            messages.append({"role": "user", "content": text})
            if save:
                self.store.add_message("user", text, source)

            reply_parts: List[str] = []
            try:
                for _ in range(MAX_TOOL_ROUNDS + 1):
                    content, tool_calls = "", []
                    think = ThinkFilter()
                    async for chunk in self.client.stream_chat(messages, TOOL_SPECS):
                        msg = chunk.get("message") or {}
                        if msg.get("content"):
                            visible = think.feed(msg["content"])
                            content += msg["content"]
                            if visible:
                                reply_parts.append(visible)
                                yield {"type": "token", "text": visible}
                        tool_calls.extend(msg.get("tool_calls") or [])
                    tail = think.flush()
                    if tail:
                        reply_parts.append(tail)
                        yield {"type": "token", "text": tail}

                    if not tool_calls:
                        break

                    messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
                    for call in tool_calls:
                        fn = call.get("function") or {}
                        name, args = fn.get("name", ""), fn.get("arguments") or {}
                        result = await self.toolbox.run(name, args)
                        log.info("tool %s(%s) -> %s", name, json.dumps(args), result)
                        messages.append({"role": "tool", "content": result, "tool_name": name})
                        yield {
                            "type": "tool",
                            "name": name,
                            "label": TOOL_LABELS.get(name, name) if not result.startswith("Error") else "⚠️ " + name,
                            "result": result,
                        }
                    if reply_parts and not reply_parts[-1].endswith(("\n", " ")):
                        reply_parts.append("\n\n")
                        yield {"type": "token", "text": "\n\n"}
            except OllamaError as e:
                log.error("LLM error: %s", e)
                yield {"type": "error", "text": f"I couldn't reach my brain ({e})."}
                return

            reply = "".join(reply_parts).strip() or "Done."
            if save:
                self.store.add_message("assistant", reply, source)
            yield {"type": "done", "text": reply}

    async def reply(self, text: str, source: str, save: bool = True) -> str:
        """Non-streaming helper: returns the final reply text (or error text)."""
        final = ""
        async for ev in self.chat(text, source, save):
            if ev["type"] in ("done", "error"):
                final = ev["text"]
        return final
