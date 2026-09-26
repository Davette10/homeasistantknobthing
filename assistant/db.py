"""SQLite storage for reminders, to-dos, memories and chat history.

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
    source      TEXT NOT NULL,     -- web | discord | cli
    created_at  TEXT NOT NULL
);
"""


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
        self._conn.executescript(SCHEMA)
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
