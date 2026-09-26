"""The agent loop: build the prompt, stream from Ollama, run tool calls, repeat."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import AsyncIterator, Dict, List, Optional

import httpx

from .config import Settings
from .db import Store
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
            f"You are {s.assistant_name}, a personal assistant for {who}. You run privately on their own "
            "computer at home and chat with them through a web app and Discord.",
            s.persona(),
            f"Right now it is {now.strftime('%A, %B %d, %Y, %I:%M %p').replace(' 0', ' ')} ({s.timezone}).",
            "What you can do:\n"
            "- Set one-time or repeating reminders with set_reminder. They get pushed to the user's phone.\n"
            "- Keep to-do and shopping lists with add_todo, list_todos, complete_todo, delete_todo.\n"
            "- Remember lasting facts about the user with remember (and forget). When they mention something "
            "worth knowing later - people in their life, birthdays, preferences, goals, routines, projects - "
            "save it without being asked. Don't save small talk or one-off details.\n"
            "- Brainstorm and suggest ideas: gifts, projects, meals, plans, things to do, ways to solve a problem. "
            "Give a few concrete, specific options tailored to what you know about them, not generic lists.",
            "Rules:\n"
            "- Actually call the tools. Never say a reminder or item is saved unless the tool result confirms it.\n"
            "- For set_reminder `when`, pass the time the way the user said it ('in 20 minutes', 'tomorrow at "
            "9am', 'friday 5pm') or as 'YYYY-MM-DD HH:MM'. If they didn't say when, ask before setting it.\n"
            "- If a tool returns an error, fix the call or ask the user; don't pretend it worked.\n"
            "- To cancel or check off something, look up its id first with list_reminders or list_todos.\n"
            "- Keep replies short - this is a chat, usually on a phone. Use a list only when it helps.",
        ]
        memories = self.store.list_memories(MAX_MEMORIES_IN_PROMPT)
        if memories:
            facts = "\n".join(f"- [#{m.id}] {m.fact}" for m in memories)
            parts.append(f"What you know about {who} (ids are for the forget tool):\n{facts}")
        else:
            parts.append(f"You don't know anything about {who} yet. Learn as you go.")
        return "\n\n".join(p for p in parts if p)

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
                        result = self.toolbox.run(name, args)
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
