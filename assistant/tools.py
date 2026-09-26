"""Tools the model can call, as Ollama/OpenAI-style JSON schemas plus their handlers.

Handlers are forgiving about argument types because small models often send "3"
instead of 3, or "mon, thu" instead of ["mon", "thu"].
"""
from __future__ import annotations

import inspect
import json
import logging
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from .config import Settings
from .db import Store
from .timeparse import TimeParseError, format_day, format_local, parse_day, parse_recurrence, parse_when
from .webtools import Web, WebError, weather_place

log = logging.getLogger(__name__)


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
    _fn(
        "create_goal",
        "Save a goal with an action plan of concrete, dated steps. Use after you've agreed on the plan with the user.",
        {
            "title": {"type": "string", "description": "The goal, e.g. 'Run a 5K without stopping'."},
            "target": {"type": "string", "description": "Target date in the user's words, e.g. 'December 1'. Optional."},
            "why": {"type": "string", "description": "Why it matters to them, in a few words. Optional."},
            "steps": {
                "type": "array",
                "description": "3-7 small, concrete steps in order.",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "due": {"type": "string", "description": "When it should be done, e.g. 'saturday', 'oct 15'."},
                    },
                    "required": ["text"],
                },
            },
        },
        ["title", "steps"],
    ),
    _fn(
        "add_goal_step",
        "Add a step to an existing goal's plan.",
        {
            "goal_id": {"type": "integer"},
            "text": {"type": "string"},
            "due": {"type": "string", "description": "Optional due date in the user's words."},
        },
        ["goal_id", "text"],
    ),
    _fn("complete_goal_step", "Mark a goal step as done (step ids are shown in your goal notes).", {"step_id": {"type": "integer"}}, ["step_id"]),
    _fn(
        "update_goal",
        "Log progress on a goal, or mark it done/dropped.",
        {
            "goal_id": {"type": "integer"},
            "progress_note": {"type": "string", "description": "What happened, e.g. 'ran 2 miles, felt good'."},
            "status": {"type": "string", "enum": ["active", "done", "dropped"]},
        },
        ["goal_id"],
    ),
    _fn(
        "web_search",
        "Search the internet for current info: news, facts, prices, opening hours, how-tos, recommendations.",
        {"query": {"type": "string"}},
        ["query"],
    ),
    _fn(
        "get_weather",
        "Current weather and 3-day forecast.",
        {"location": {"type": "string", "description": "City, e.g. 'Boston, MA'. Omit for the user's home."}},
        [],
    ),
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

    def __init__(self, store: Store, settings: Settings, web: Optional[Web] = None):
        self.store = store
        self.settings = settings
        self.web = web or Web(settings.searxng_url)
        self.handlers: Dict[str, Callable[[dict], Any]] = {
            "set_reminder": self.set_reminder,
            "list_reminders": self.list_reminders,
            "cancel_reminder": self.cancel_reminder,
            "add_todo": self.add_todo,
            "list_todos": self.list_todos,
            "complete_todo": self.complete_todo,
            "delete_todo": self.delete_todo,
            "remember": self.remember,
            "forget": self.forget,
            "create_goal": self.create_goal,
            "add_goal_step": self.add_goal_step,
            "complete_goal_step": self.complete_goal_step,
            "update_goal": self.update_goal,
            "web_search": self.web_search,
            "get_weather": self.get_weather,
        }

    async def run(self, name: str, args: Any) -> str:
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
            result = handler(args)
            if inspect.isawaitable(result):
                result = await result
            return result
        except (ToolError, TimeParseError, WebError) as e:
            return f"Error: {e}"
        except Exception:
            log.exception("tool %s crashed", name)
            return f"Error: {name} failed unexpectedly."

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


    # --- goals ---------------------------------------------------------------

    def _due(self, text) -> Optional[datetime]:
        """Lenient date for plan steps: a bad date shouldn't sink the whole plan."""
        if not text or not str(text).strip():
            return None
        try:
            return parse_day(str(text), self.settings.tz)
        except TimeParseError:
            return None

    def create_goal(self, args: dict) -> str:
        title = _str(args, "title")
        steps = args.get("steps") or []
        if isinstance(steps, str):
            try:
                steps = json.loads(steps)
            except json.JSONDecodeError:
                steps = [ln.strip(" -*") for ln in steps.splitlines() if ln.strip()]
        goal = self.store.add_goal(title, (args.get("why") or None), self._due(args.get("target")))
        for step in steps:
            if isinstance(step, dict):
                text, due = str(step.get("text") or step.get("step") or "").strip(), step.get("due")
            else:
                text, due = str(step).strip(), None
            if text:
                self.store.add_goal_step(goal.id, text, self._due(due))
        return f"Saved goal #{goal.id}.\n" + self.describe_goal(goal.id)

    def describe_goal(self, gid: int) -> str:
        g = self.store.get_goal(gid)
        if g is None:
            return "Error: no goal with that id."
        head = f"Goal #{g.id}: {g.title}"
        if g.target_at:
            head += f" (target {format_day(g.target_at, self.settings.tz)})"
        lines = [head, f"{g.done_count}/{len(g.steps)} steps done"]
        for st in g.steps:
            due = f" - due {format_day(st.due_at, self.settings.tz)}" if st.due_at else ""
            lines.append(f"  [{'x' if st.done else ' '}] step #{st.id} {st.text}{due}")
        return "\n".join(lines)

    def add_goal_step(self, args: dict) -> str:
        step = self.store.add_goal_step(_int(args, "goal_id"), _str(args, "text"), self._due(args.get("due")))
        if step is None:
            return "Error: no goal with that id."
        return f"Added step #{step.id} to goal #{step.goal_id}."

    def complete_goal_step(self, args: dict) -> str:
        step = self.store.set_step_done(_int(args, "step_id"), True)
        if step is None:
            return "Error: no step with that id."
        g = self.store.get_goal(step.goal_id)
        msg = f"Checked off '{step.text}'. Goal '{g.title}' is now {g.done_count}/{len(g.steps)} done."
        if g.open_steps:
            msg += f" Next step: #{g.open_steps[0].id} {g.open_steps[0].text}."
        else:
            msg += " That was the last step - ask if the goal is complete."
        return msg

    def update_goal(self, args: dict) -> str:
        gid = _int(args, "goal_id")
        g = self.store.get_goal(gid)
        if g is None:
            return "Error: no goal with that id."
        out = []
        note = (args.get("progress_note") or "").strip()
        if note:
            self.store.add_goal_note(gid, note, datetime.now(self.settings.tz))
            out.append("Progress logged.")
        status = (args.get("status") or "").strip().lower()
        if status in ("active", "done", "dropped") and status != g.status:
            self.store.set_goal_status(gid, status)
            out.append({"done": "Goal marked complete! 🎉", "dropped": "Goal dropped.", "active": "Goal reactivated."}[status])
        return " ".join(out) or "Nothing to update - pass progress_note or status."

    # --- web -------------------------------------------------------------------

    async def web_search(self, args: dict) -> str:
        return await self.web.search_summary(_str(args, "query"))

    async def get_weather(self, args: dict) -> str:
        return await self.web.weather(weather_place(self.settings.weather_location, args.get("location")))


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
    "create_goal": "🎯 Goal plan saved",
    "add_goal_step": "🎯 Step added",
    "complete_goal_step": "🎯 Step done",
    "update_goal": "🎯 Goal updated",
    "web_search": "🔎 Searched the web",
    "get_weather": "🌤️ Checked weather",
}
