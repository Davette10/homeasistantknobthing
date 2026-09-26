"""SQLite storage for reminders, to-dos, goals, memories, chat history and small settings.

All timestamps are stored as UTC ISO-8601 strings.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .timeparse import Recurrence

UTC = timezone.utc

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    text         TEXT NOT NULL,
    due_at       TEXT NOT NULL,
    recurrence   TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending | fired | done | cancelled
    created_at   TEXT NOT NULL,
    fired_at     TEXT
);
CREATE INDEX IF NOT EXISTS reminders_due ON reminders(status, due_at);

CREATE TABLE IF NOT EXISTS todos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    text        TEXT NOT NULL,
    list_name   TEXT NOT NULL DEFAULT 'to-do',
    done        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    done_at     TEXT
);

CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fact        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    role        TEXT NOT NULL,     -- user | assistant
    content     TEXT NOT NULL,
    source      TEXT NOT NULL,     -- web | discord | cli | checkin
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS goals (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    title             TEXT NOT NULL,
    why               TEXT,
    target_at         TEXT,
    status            TEXT NOT NULL DEFAULT 'active',  -- active | done | dropped
    notes             TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    last_activity_at  TEXT NOT NULL,
    checked_in_at     TEXT
);

CREATE TABLE IF NOT EXISTS goal_steps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id     INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    text        TEXT NOT NULL,
    due_at      TEXT,
    done        INTEGER NOT NULL DEFAULT 0,
    done_at     TEXT,
    nudged_at   TEXT,
    position    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS kv (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);
"""

# Columns added after the first release; created on startup for older databases.
MIGRATIONS = [
    ("reminders", "followed_up", "INTEGER NOT NULL DEFAULT 0"),
]


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _dt(s: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(s) if s else None


@dataclass
class Reminder:
    id: int
    text: str
    due_at: datetime
    recurrence: Optional[Recurrence]
    status: str
    created_at: datetime
    fired_at: Optional[datetime]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Reminder":
        return cls(
            id=row["id"],
            text=row["text"],
            due_at=_dt(row["due_at"]),
            recurrence=Recurrence.from_dict(json.loads(row["recurrence"])) if row["recurrence"] else None,
            status=row["status"],
            created_at=_dt(row["created_at"]),
            fired_at=_dt(row["fired_at"]),
        )


@dataclass
class Todo:
    id: int
    text: str
    list_name: str
    done: bool
    created_at: datetime


@dataclass
class GoalStep:
    id: int
    goal_id: int
    text: str
    due_at: Optional[datetime]
    done: bool
    nudged_at: Optional[datetime]


@dataclass
class Goal:
    id: int
    title: str
    why: Optional[str]
    target_at: Optional[datetime]
    status: str
    notes: str
    created_at: datetime
    last_activity_at: datetime
    checked_in_at: Optional[datetime]
    steps: List[GoalStep]

    @property
    def done_count(self) -> int:
        return sum(1 for s in self.steps if s.done)

    @property
    def open_steps(self) -> List[GoalStep]:
        return [s for s in self.steps if not s.done]


@dataclass
class Memory:
    id: int
    fact: str
    created_at: datetime


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        for table, column, decl in MIGRATIONS:
            cols = [r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")]
            if column not in cols:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        self._conn.commit()
        self._lock = threading.Lock()

    def _exec(self, sql: str, params=()) -> sqlite3.Cursor:
        with self._lock, self._conn:
            return self._conn.execute(sql, params)

    def _all(self, sql: str, params=()) -> List[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _one(self, sql: str, params=()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    # --- reminders -----------------------------------------------------------

    def add_reminder(self, text: str, due_at: datetime, recurrence: Optional[Recurrence] = None) -> Reminder:
        cur = self._exec(
            "INSERT INTO reminders(text, due_at, recurrence, created_at) VALUES (?,?,?,?)",
            (text, _iso(due_at), json.dumps(recurrence.to_dict()) if recurrence else None, _iso(now_utc())),
        )
        return self.get_reminder(cur.lastrowid)

    def get_reminder(self, rid: int) -> Optional[Reminder]:
        row = self._one("SELECT * FROM reminders WHERE id=?", (rid,))
        return Reminder.from_row(row) if row else None

    def upcoming_reminders(self, limit: int = 50) -> List[Reminder]:
        rows = self._all("SELECT * FROM reminders WHERE status='pending' ORDER BY due_at LIMIT ?", (limit,))
        return [Reminder.from_row(r) for r in rows]

    def recent_fired_reminders(self, limit: int = 10) -> List[Reminder]:
        rows = self._all("SELECT * FROM reminders WHERE status='fired' ORDER BY fired_at DESC LIMIT ?", (limit,))
        return [Reminder.from_row(r) for r in rows]

    def due_reminders(self, now: datetime) -> List[Reminder]:
        rows = self._all(
            "SELECT * FROM reminders WHERE status='pending' AND due_at <= ? ORDER BY due_at", (_iso(now),)
        )
        return [Reminder.from_row(r) for r in rows]

    def mark_fired(self, rid: int, fired_at: datetime, next_due: Optional[datetime]) -> None:
        """Record a delivery. Repeating reminders move to their next time and stay pending."""
        if next_due is not None:
            self._exec("UPDATE reminders SET due_at=?, fired_at=? WHERE id=?", (_iso(next_due), _iso(fired_at), rid))
        else:
            self._exec("UPDATE reminders SET status='fired', fired_at=? WHERE id=?", (_iso(fired_at), rid))

    def snooze_reminder(self, rid: int, until: datetime) -> Optional[Reminder]:
        """Snoozing a one-off re-arms it; a repeating one gets a one-off copy so its schedule is untouched."""
        r = self.get_reminder(rid)
        if r is None:
            return None
        if r.recurrence:
            return self.add_reminder(r.text, until)
        self._exec("UPDATE reminders SET status='pending', due_at=? WHERE id=?", (_iso(until), rid))
        return self.get_reminder(rid)

    def unacknowledged_reminders(self, fired_before: datetime) -> List[Reminder]:
        """One-off reminders that fired a while ago and were never marked done or snoozed."""
        rows = self._all(
            "SELECT * FROM reminders WHERE status='fired' AND followed_up=0 AND recurrence IS NULL AND fired_at <= ?"
            " ORDER BY fired_at",
            (_iso(fired_before),),
        )
        return [Reminder.from_row(r) for r in rows]

    def mark_followed_up(self, rid: int) -> None:
        self._exec("UPDATE reminders SET followed_up=1 WHERE id=?", (rid,))

    def reminders_between(self, start: datetime, end: datetime) -> List[Reminder]:
        rows = self._all(
            "SELECT * FROM reminders WHERE status='pending' AND due_at >= ? AND due_at < ? ORDER BY due_at",
            (_iso(start), _iso(end)),
        )
        return [Reminder.from_row(r) for r in rows]

    def complete_reminder(self, rid: int) -> bool:
        cur = self._exec("UPDATE reminders SET status='done' WHERE id=? AND status='fired'", (rid,))
        return cur.rowcount > 0

    def cancel_reminder(self, rid: int) -> Optional[Reminder]:
        r = self.get_reminder(rid)
        if r is None or r.status in ("cancelled", "done"):
            return None
        self._exec("UPDATE reminders SET status='cancelled' WHERE id=?", (rid,))
        return r

    # --- to-dos --------------------------------------------------------------

    @staticmethod
    def _todo(row: sqlite3.Row) -> Todo:
        return Todo(row["id"], row["text"], row["list_name"], bool(row["done"]), _dt(row["created_at"]))

    def add_todo(self, text: str, list_name: str = "to-do") -> Todo:
        cur = self._exec(
            "INSERT INTO todos(text, list_name, created_at) VALUES (?,?,?)",
            (text, normalize_list(list_name), _iso(now_utc())),
        )
        return self.get_todo(cur.lastrowid)

    def get_todo(self, tid: int) -> Optional[Todo]:
        row = self._one("SELECT * FROM todos WHERE id=?", (tid,))
        return self._todo(row) if row else None

    def list_todos(self, list_name: Optional[str] = None, include_done: bool = False) -> List[Todo]:
        sql, params = "SELECT * FROM todos WHERE 1=1", []
        if list_name:
            sql += " AND list_name=?"
            params.append(normalize_list(list_name))
        if not include_done:
            sql += " AND done=0"
        sql += " ORDER BY done, list_name, id"
        return [self._todo(r) for r in self._all(sql, params)]

    def todo_lists(self) -> List[str]:
        return [r[0] for r in self._all("SELECT DISTINCT list_name FROM todos WHERE done=0 ORDER BY list_name")]

    def set_todo_done(self, tid: int, done: bool = True) -> Optional[Todo]:
        self._exec(
            "UPDATE todos SET done=?, done_at=? WHERE id=?",
            (1 if done else 0, _iso(now_utc()) if done else None, tid),
        )
        return self.get_todo(tid)

    def delete_todo(self, tid: int) -> Optional[Todo]:
        t = self.get_todo(tid)
        if t:
            self._exec("DELETE FROM todos WHERE id=?", (tid,))
        return t

    def clear_done_todos(self) -> int:
        return self._exec("DELETE FROM todos WHERE done=1").rowcount

    # --- goals ---------------------------------------------------------------

    def _goal(self, row: sqlite3.Row) -> Goal:
        steps = [
            GoalStep(r["id"], r["goal_id"], r["text"], _dt(r["due_at"]), bool(r["done"]), _dt(r["nudged_at"]))
            for r in self._all(
                "SELECT * FROM goal_steps WHERE goal_id=? ORDER BY position, id", (row["id"],)
            )
        ]
        return Goal(
            id=row["id"], title=row["title"], why=row["why"], target_at=_dt(row["target_at"]),
            status=row["status"], notes=row["notes"], created_at=_dt(row["created_at"]),
            last_activity_at=_dt(row["last_activity_at"]), checked_in_at=_dt(row["checked_in_at"]), steps=steps,
        )

    def add_goal(self, title: str, why: Optional[str] = None, target_at: Optional[datetime] = None) -> Goal:
        now = _iso(now_utc())
        cur = self._exec(
            "INSERT INTO goals(title, why, target_at, created_at, last_activity_at) VALUES (?,?,?,?,?)",
            (title, why, _iso(target_at) if target_at else None, now, now),
        )
        return self.get_goal(cur.lastrowid)

    def get_goal(self, gid: int) -> Optional[Goal]:
        row = self._one("SELECT * FROM goals WHERE id=?", (gid,))
        return self._goal(row) if row else None

    def list_goals(self, include_closed: bool = False) -> List[Goal]:
        sql = "SELECT * FROM goals" + ("" if include_closed else " WHERE status='active'")
        sql += " ORDER BY status='active' DESC, COALESCE(target_at, '9999'), id"
        return [self._goal(r) for r in self._all(sql)]

    def touch_goal(self, gid: int) -> None:
        self._exec("UPDATE goals SET last_activity_at=? WHERE id=?", (_iso(now_utc()), gid))

    def set_goal_status(self, gid: int, status: str) -> Optional[Goal]:
        self._exec("UPDATE goals SET status=?, last_activity_at=? WHERE id=?", (status, _iso(now_utc()), gid))
        return self.get_goal(gid)

    def add_goal_note(self, gid: int, note: str, when: datetime) -> None:
        g = self.get_goal(gid)
        if g is None:
            return
        line = f"{when.strftime('%b %d')}: {note}"
        notes = "\n".join((g.notes.splitlines() + [line])[-10:])  # keep the last 10
        self._exec("UPDATE goals SET notes=?, last_activity_at=? WHERE id=?", (notes, _iso(now_utc()), gid))

    def mark_goal_checked_in(self, gid: int, when: Optional[datetime] = None) -> None:
        self._exec("UPDATE goals SET checked_in_at=? WHERE id=?", (_iso(when or now_utc()), gid))

    def delete_goal(self, gid: int) -> Optional[Goal]:
        g = self.get_goal(gid)
        if g:
            self._exec("DELETE FROM goal_steps WHERE goal_id=?", (gid,))
            self._exec("DELETE FROM goals WHERE id=?", (gid,))
        return g

    def add_goal_step(self, gid: int, text: str, due_at: Optional[datetime] = None) -> Optional[GoalStep]:
        if self._one("SELECT id FROM goals WHERE id=?", (gid,)) is None:
            return None
        pos = self._one("SELECT COALESCE(MAX(position), 0) + 1 AS p FROM goal_steps WHERE goal_id=?", (gid,))["p"]
        cur = self._exec(
            "INSERT INTO goal_steps(goal_id, text, due_at, position) VALUES (?,?,?,?)",
            (gid, text, _iso(due_at) if due_at else None, pos),
        )
        self.touch_goal(gid)
        return self.get_step(cur.lastrowid)

    def get_step(self, sid: int) -> Optional[GoalStep]:
        r = self._one("SELECT * FROM goal_steps WHERE id=?", (sid,))
        return GoalStep(r["id"], r["goal_id"], r["text"], _dt(r["due_at"]), bool(r["done"]), _dt(r["nudged_at"])) if r else None

    def set_step_done(self, sid: int, done: bool = True) -> Optional[GoalStep]:
        step = self.get_step(sid)
        if step is None:
            return None
        self._exec(
            "UPDATE goal_steps SET done=?, done_at=? WHERE id=?",
            (1 if done else 0, _iso(now_utc()) if done else None, sid),
        )
        self.touch_goal(step.goal_id)
        return self.get_step(sid)

    def delete_step(self, sid: int) -> Optional[GoalStep]:
        step = self.get_step(sid)
        if step:
            self._exec("DELETE FROM goal_steps WHERE id=?", (sid,))
        return step

    def overdue_steps(self, now: datetime) -> List[GoalStep]:
        rows = self._all(
            "SELECT s.* FROM goal_steps s JOIN goals g ON g.id=s.goal_id "
            "WHERE g.status='active' AND s.done=0 AND s.due_at IS NOT NULL AND s.due_at < ? ORDER BY s.due_at",
            (_iso(now),),
        )
        return [GoalStep(r["id"], r["goal_id"], r["text"], _dt(r["due_at"]), False, _dt(r["nudged_at"])) for r in rows]

    def mark_step_nudged(self, sid: int, when: Optional[datetime] = None) -> None:
        self._exec("UPDATE goal_steps SET nudged_at=? WHERE id=?", (_iso(when or now_utc()), sid))

    # --- key/value (check-in bookkeeping, settings) ----------------------------

    def get_kv(self, key: str, default=None):
        row = self._one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set_kv(self, key: str, value) -> None:
        self._exec(
            "INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    # --- memories ------------------------------------------------------------

    def add_memory(self, fact: str) -> Memory:
        existing = self._one("SELECT * FROM memories WHERE lower(fact)=lower(?)", (fact,))
        if existing:
            return Memory(existing["id"], existing["fact"], _dt(existing["created_at"]))
        cur = self._exec("INSERT INTO memories(fact, created_at) VALUES (?,?)", (fact, _iso(now_utc())))
        return Memory(cur.lastrowid, fact, now_utc())

    def list_memories(self, limit: int = 200) -> List[Memory]:
        rows = self._all("SELECT * FROM memories ORDER BY id DESC LIMIT ?", (limit,))
        return [Memory(r["id"], r["fact"], _dt(r["created_at"])) for r in reversed(rows)]

    def delete_memory(self, mid: int) -> Optional[Memory]:
        row = self._one("SELECT * FROM memories WHERE id=?", (mid,))
        if not row:
            return None
        self._exec("DELETE FROM memories WHERE id=?", (mid,))
        return Memory(row["id"], row["fact"], _dt(row["created_at"]))

    # --- chat history --------------------------------------------------------

    def add_message(self, role: str, content: str, source: str) -> None:
        self._exec(
            "INSERT INTO messages(role, content, source, created_at) VALUES (?,?,?,?)",
            (role, content, source, _iso(now_utc())),
        )

    def recent_messages(self, limit: int) -> List[dict]:
        rows = self._all("SELECT * FROM messages ORDER BY id DESC LIMIT ?", (limit,))
        return [
            {"role": r["role"], "content": r["content"], "source": r["source"], "created_at": r["created_at"]}
            for r in reversed(rows)
        ]

    def clear_messages(self) -> None:
        self._exec("DELETE FROM messages")


def normalize_list(name: Optional[str]) -> str:
    name = (name or "").strip().lower()
    if name in ("", "todo", "todos", "to do", "to-do", "to-dos", "tasks", "task", "default", "main"):
        return "to-do"
    if name.endswith(" list"):
        name = name[: -len(" list")]
    if name in ("grocery", "groceries"):
        return "groceries"
    return name
