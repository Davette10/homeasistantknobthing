"""Tools the model can call, as Ollama/OpenAI-style JSON schemas plus their handlers.

Handlers are forgiving about argument types because small models often send "3"
instead of 3, or "mon, thu" instead of ["mon", "thu"].
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List

from .config import Settings
from .db import Store
from .timeparse import TimeParseError, format_local, parse_recurrence, parse_when


def _fn(name: str, description: str, properties: dict, required: List[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


TOOL_SPECS = [
    _fn(
        "set_reminder",
        "Schedule a reminder that will be sent to the user's phone at a given time. Can repeat.",
        {
            "text": {"type": "string", "description": "What to remind them about, e.g. 'take out the trash'."},
            "when": {
                "type": "string",
                "description": "When to (first) remind, in the user's words: 'in 20 minutes', 'tomorrow at 9am', "
                "'friday 5pm', or 'YYYY-MM-DD HH:MM'.",
            },
            "repeat": {
                "type": "string",
                "enum": ["none", "hourly", "daily", "weekdays", "weekends", "weekly", "monthly", "yearly"],
                "description": "How often it repeats. Use 'none' for a one-time reminder.",
            },
            "every": {"type": "integer", "description": "Optional interval, e.g. 2 with 'weekly' = every 2 weeks."},
            "days": {
                "type": "array",
                "items": {"type": "string", "enum": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
                "description": "For weekly reminders on specific days, e.g. ['mon', 'thu'].",
            },
        },
        ["text", "when"],
    ),
    _fn("list_reminders", "List the user's upcoming reminders.", {}, []),
    _fn(
        "cancel_reminder",
        "Cancel/delete a reminder by its id (get ids from list_reminders).",
        {"id": {"type": "integer"}},
        ["id"],
    ),
    _fn(
        "add_todo",
        "Add one item to a to-do list. Call once per item.",
        {
            "item": {"type": "string"},
            "list": {"type": "string", "description": "List name, e.g. 'groceries'. Defaults to 'to-do'."},
        },
        ["item"],
    ),
    _fn(
        "list_todos",
        "Show items on the user's to-do lists.",
        {"list": {"type": "string", "description": "Only this list. Omit for all lists."}},
        [],
    ),
    _fn("complete_todo", "Check off a to-do item by id.", {"id": {"type": "integer"}}, ["id"]),
    _fn("delete_todo", "Remove a to-do item by id without completing it.", {"id": {"type": "integer"}}, ["id"]),
    _fn(
        "remember",
        "Save a lasting fact about the user (name, birthdays, preferences, goals, people in their life, routines).",
        {"fact": {"type": "string", "description": "Short standalone fact, e.g. 'Sister Maya's birthday is May 3'."}},
        ["fact"],
    ),
    _fn("forget", "Delete a saved fact about the user by its id.", {"id": {"type": "integer"}}, ["id"]),
]


class ToolError(Exception):
    pass


def _int(args: dict, key: str = "id") -> int:
    try:
        return int(str(args.get(key)).strip().lstrip("#"))
    except (TypeError, ValueError):
        raise ToolError(f"'{key}' must be a number")


def _str(args: dict, key: str) -> str:
    v = args.get(key)
    if v is None or not str(v).strip():
        raise ToolError(f"missing '{key}'")
    return str(v).strip()


class Toolbox:
    """Runs tool calls against the store. Every handler returns a short string for the model."""

    def __init__(self, store: Store, settings: Settings):
        self.store = store
        self.settings = settings
        self.handlers: Dict[str, Callable[[dict], str]] = {
            "set_reminder": self.set_reminder,
            "list_reminders": self.list_reminders,
            "cancel_reminder": self.cancel_reminder,
            "add_todo": self.add_todo,
            "list_todos": self.list_todos,
            "complete_todo": self.complete_todo,
            "delete_todo": self.delete_todo,
            "remember": self.remember,
            "forget": self.forget,
        }

    def run(self, name: str, args: Any) -> str:
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError:
                return f"Error: arguments for {name} were not valid JSON."
        args = args if isinstance(args, dict) else {}
        handler = self.handlers.get(name)
        if handler is None:
            return f"Error: there is no tool called {name!r}."
        try:
            return handler(args)
        except (ToolError, TimeParseError) as e:
            return f"Error: {e}"

    def _when(self, dt) -> str:
        return format_local(dt, self.settings.tz)

    # --- reminders -----------------------------------------------------------

    def set_reminder(self, args: dict) -> str:
        text = _str(args, "text")
        tz = self.settings.tz
        recurrence = parse_recurrence(args.get("repeat"), args.get("every"), args.get("days"))
        due = parse_when(_str(args, "when"), tz)
        if recurrence:
            due = recurrence.first(due, tz)
        r = self.store.add_reminder(text, due, recurrence)
        msg = f"Reminder #{r.id} set for {self._when(r.due_at)}: {text}"
        if recurrence:
            msg += f" (repeats {recurrence.describe()})"
        return msg

    def list_reminders(self, args: dict) -> str:
        items = self.store.upcoming_reminders(25)
        if not items:
            return "No upcoming reminders."
        lines = []
        for r in items:
            line = f"#{r.id} {self._when(r.due_at)} - {r.text}"
            if r.recurrence:
                line += f" (repeats {r.recurrence.describe()})"
            lines.append(line)
        return "\n".join(lines)

    def cancel_reminder(self, args: dict) -> str:
        r = self.store.cancel_reminder(_int(args))
        if r is None:
            return "Error: no active reminder with that id. Call list_reminders to see ids."
        return f"Cancelled reminder #{r.id}: {r.text}"

    # --- to-dos --------------------------------------------------------------

    def add_todo(self, args: dict) -> str:
        t = self.store.add_todo(_str(args, "item"), args.get("list") or "to-do")
        return f"Added #{t.id} '{t.text}' to the {t.list_name} list."

    def list_todos(self, args: dict) -> str:
        items = self.store.list_todos(args.get("list") or None)
        if not items:
            return "That list is empty." if args.get("list") else "All to-do lists are empty."
        out, current = [], None
        for t in items:
            if t.list_name != current:
                current = t.list_name
                out.append(f"{current}:")
            out.append(f"  #{t.id} {t.text}")
        return "\n".join(out)

    def complete_todo(self, args: dict) -> str:
        t = self.store.get_todo(_int(args))
        if t is None:
            return "Error: no to-do with that id. Call list_todos to see ids."
        self.store.set_todo_done(t.id, True)
        return f"Checked off '{t.text}'."

    def delete_todo(self, args: dict) -> str:
        t = self.store.delete_todo(_int(args))
        if t is None:
            return "Error: no to-do with that id. Call list_todos to see ids."
        return f"Removed '{t.text}' from {t.list_name}."

    # --- memory --------------------------------------------------------------

    def remember(self, args: dict) -> str:
        m = self.store.add_memory(_str(args, "fact"))
        return f"Saved memory #{m.id}: {m.fact}"

    def forget(self, args: dict) -> str:
        m = self.store.delete_memory(_int(args))
        if m is None:
            return "Error: no memory with that id."
        return f"Forgot: {m.fact}"


# Short human labels shown as chips in the web UI / Discord when a tool runs.
TOOL_LABELS = {
    "set_reminder": "⏰ Reminder set",
    "list_reminders": "📋 Checked reminders",
    "cancel_reminder": "🗑️ Reminder cancelled",
    "add_todo": "✅ Added to list",
    "list_todos": "📋 Checked lists",
    "complete_todo": "☑️ Checked off",
    "delete_todo": "🗑️ Removed item",
    "remember": "🧠 Remembered",
    "forget": "🧠 Forgot",
}
