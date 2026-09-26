"""Web UI + JSON API. Single user, password login, signed session cookie."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from datetime import timedelta
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .agent import Agent
from .coach import MODES, CheckinConfig
from .config import Settings
from .db import Store, now_utc
from .scheduler import Notifier
from .timeparse import TimeParseError, format_day, format_local, parse_day, parse_recurrence, parse_when

STATIC = Path(__file__).parent / "static"
COOKIE = "assistant_session"
SESSION_DAYS = 30


class LoginBody(BaseModel):
    password: str


class ChatBody(BaseModel):
    message: str


class TodoBody(BaseModel):
    text: str
    list: Optional[str] = None


class MemoryBody(BaseModel):
    fact: str


class ReminderBody(BaseModel):
    text: str
    when: str
    repeat: Optional[str] = None


class SnoozeBody(BaseModel):
    minutes: int = 10


class GoalBody(BaseModel):
    title: str
    target: Optional[str] = None
    why: Optional[str] = None


class StepBody(BaseModel):
    text: str
    due: Optional[str] = None


class StatusBody(BaseModel):
    status: str


class CheckinBody(BaseModel):
    mode: str
    morning: str
    midday: str
    evening: str
    quiet_hours: str
    weather_location: str = ""


def create_app(settings: Settings, store: Store, agent: Agent, notifier: Notifier) -> FastAPI:
    app = FastAPI(title=settings.assistant_name, docs_url=None, redoc_url=None)
    tz = settings.tz
    failed_logins = {"count": 0, "until": 0.0}

    # --- sessions ------------------------------------------------------------

    def sign(expiry: int) -> str:
        mac = hmac.new(settings.secret_key.encode(), str(expiry).encode(), hashlib.sha256).hexdigest()
        return f"{expiry}.{mac}"

    def valid(token: Optional[str]) -> bool:
        if not token or "." not in token:
            return False
        expiry, _ = token.split(".", 1)
        if not expiry.isdigit() or int(expiry) < time.time():
            return False
        return hmac.compare_digest(token, sign(int(expiry)))

    def require_auth(request: Request) -> None:
        if not valid(request.cookies.get(COOKIE)):
            raise HTTPException(401, "not logged in")

    auth = [Depends(require_auth)]

    # --- pages & auth --------------------------------------------------------

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/api/me")
    async def me(request: Request):
        return {
            "authed": valid(request.cookies.get(COOKIE)),
            "name": settings.assistant_name,
            "user": settings.user_name,
            "discord": bool(settings.discord_token and settings.discord_owner_id),
        }

    @app.post("/api/login")
    async def login(body: LoginBody, request: Request, response: Response):
        if time.time() < failed_logins["until"]:
            raise HTTPException(429, "Too many attempts. Wait a minute and try again.")
        ok = bool(settings.web_password) and hmac.compare_digest(body.password.encode(), settings.web_password.encode())
        if not ok:
            failed_logins["count"] += 1
            if failed_logins["count"] >= 5:
                failed_logins.update(count=0, until=time.time() + 60)
            await asyncio.sleep(1)
            raise HTTPException(401, "Wrong password.")
        failed_logins["count"] = 0
        expiry = int(time.time() + SESSION_DAYS * 86400)
        response.set_cookie(
            COOKIE, sign(expiry), max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax",
            secure=request.url.scheme == "https",
        )
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(response: Response):
        response.delete_cookie(COOKIE)
        return {"ok": True}

    @app.get("/api/health", dependencies=auth)
    async def health():
        return await agent.client.health()

    # --- chat ----------------------------------------------------------------

    @app.get("/api/history", dependencies=auth)
    async def history():
        return store.recent_messages(100)

    @app.delete("/api/history", dependencies=auth)
    async def clear_history():
        store.clear_messages()
        return {"ok": True}

    @app.post("/api/chat", dependencies=auth)
    async def chat(body: ChatBody):
        text = body.message.strip()
        if not text:
            raise HTTPException(400, "empty message")

        async def events():
            async for ev in agent.chat(text, source="web"):
                if ev["type"] == "tool":
                    notifier.publish_web({"type": "changed"})
                yield json.dumps(ev) + "\n"

        return StreamingResponse(events(), media_type="application/x-ndjson")

    @app.get("/api/events", dependencies=auth)
    async def events(request: Request):
        q = notifier.subscribe_web()

        async def stream():
            try:
                yield "retry: 5000\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=20)
                        yield f"data: {json.dumps(ev)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            finally:
                notifier.unsubscribe_web(q)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    # --- reminders -----------------------------------------------------------

    def reminder_json(r):
        return {
            "id": r.id,
            "text": r.text,
            "due_at": r.due_at.isoformat(),
            "when": format_local(r.due_at, tz),
            "repeat": r.recurrence.describe() if r.recurrence else None,
            "status": r.status,
        }

    @app.get("/api/reminders", dependencies=auth)
    async def reminders():
        return {
            "upcoming": [reminder_json(r) for r in store.upcoming_reminders(100)],
            "recent": [reminder_json(r) for r in store.recent_fired_reminders(10)],
        }

    @app.post("/api/reminders", dependencies=auth)
    async def add_reminder(body: ReminderBody):
        try:
            rec = parse_recurrence(body.repeat)
            due = parse_when(body.when, tz)
            if rec:
                due = rec.first(due, tz)
        except TimeParseError as e:
            raise HTTPException(400, str(e))
        return reminder_json(store.add_reminder(body.text.strip(), due, rec))

    @app.delete("/api/reminders/{rid}", dependencies=auth)
    async def cancel_reminder(rid: int):
        store.cancel_reminder(rid)
        return {"ok": True}

    @app.post("/api/reminders/{rid}/done", dependencies=auth)
    async def done_reminder(rid: int):
        store.complete_reminder(rid)
        return {"ok": True}

    @app.post("/api/reminders/{rid}/snooze", dependencies=auth)
    async def snooze_reminder(rid: int, body: SnoozeBody):
        r = store.snooze_reminder(rid, now_utc() + timedelta(minutes=max(1, body.minutes)))
        if r is None:
            raise HTTPException(404, "no such reminder")
        return reminder_json(r)

    # --- to-dos --------------------------------------------------------------

    def todo_json(t):
        return {"id": t.id, "text": t.text, "list": t.list_name, "done": t.done}

    @app.get("/api/todos", dependencies=auth)
    async def todos():
        return [todo_json(t) for t in store.list_todos(include_done=True)]

    @app.post("/api/todos", dependencies=auth)
    async def add_todo(body: TodoBody):
        if not body.text.strip():
            raise HTTPException(400, "empty item")
        return todo_json(store.add_todo(body.text.strip(), body.list or "to-do"))

    @app.post("/api/todos/{tid}/toggle", dependencies=auth)
    async def toggle_todo(tid: int):
        t = store.get_todo(tid)
        if t is None:
            raise HTTPException(404, "no such item")
        return todo_json(store.set_todo_done(tid, not t.done))

    @app.delete("/api/todos/{tid}", dependencies=auth)
    async def delete_todo(tid: int):
        store.delete_todo(tid)
        return {"ok": True}

    @app.post("/api/todos/clear-done", dependencies=auth)
    async def clear_done():
        return {"removed": store.clear_done_todos()}

    # --- memories ------------------------------------------------------------

    @app.get("/api/memories", dependencies=auth)
    async def memories():
        return [{"id": m.id, "fact": m.fact} for m in store.list_memories()]

    @app.post("/api/memories", dependencies=auth)
    async def add_memory(body: MemoryBody):
        if not body.fact.strip():
            raise HTTPException(400, "empty fact")
        m = store.add_memory(body.fact.strip())
        return {"id": m.id, "fact": m.fact}

    @app.delete("/api/memories/{mid}", dependencies=auth)
    async def delete_memory(mid: int):
        store.delete_memory(mid)
        return {"ok": True}

    # --- goals ---------------------------------------------------------------

    def goal_json(g):
        return {
            "id": g.id, "title": g.title, "why": g.why, "status": g.status,
            "target": format_day(g.target_at, tz) if g.target_at else None,
            "done": g.done_count, "total": len(g.steps),
            "notes": g.notes.splitlines()[-3:] if g.notes else [],
            "steps": [
                {"id": st.id, "text": st.text, "done": st.done,
                 "due": format_day(st.due_at, tz) if st.due_at else None,
                 "overdue": bool(st.due_at and not st.done and st.due_at.astimezone(tz).date() < now_utc().astimezone(tz).date())}
                for st in g.steps
            ],
        }

    def parse_optional(text: Optional[str]):
        if not text or not text.strip():
            return None
        try:
            return parse_day(text, tz)
        except TimeParseError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/goals", dependencies=auth)
    async def goals():
        return [goal_json(g) for g in store.list_goals(include_closed=True)]

    @app.post("/api/goals", dependencies=auth)
    async def add_goal(body: GoalBody):
        if not body.title.strip():
            raise HTTPException(400, "empty goal")
        return goal_json(store.add_goal(body.title.strip(), body.why, parse_optional(body.target)))

    @app.post("/api/goals/{gid}/status", dependencies=auth)
    async def goal_status(gid: int, body: StatusBody):
        if body.status not in ("active", "done", "dropped"):
            raise HTTPException(400, "bad status")
        g = store.set_goal_status(gid, body.status)
        if g is None:
            raise HTTPException(404, "no such goal")
        return goal_json(g)

    @app.delete("/api/goals/{gid}", dependencies=auth)
    async def delete_goal(gid: int):
        store.delete_goal(gid)
        return {"ok": True}

    @app.post("/api/goals/{gid}/steps", dependencies=auth)
    async def add_step(gid: int, body: StepBody):
        if not body.text.strip():
            raise HTTPException(400, "empty step")
        if store.add_goal_step(gid, body.text.strip(), parse_optional(body.due)) is None:
            raise HTTPException(404, "no such goal")
        return goal_json(store.get_goal(gid))

    @app.post("/api/steps/{sid}/toggle", dependencies=auth)
    async def toggle_step(sid: int):
        st = store.get_step(sid)
        if st is None:
            raise HTTPException(404, "no such step")
        store.set_step_done(sid, not st.done)
        return goal_json(store.get_goal(st.goal_id))

    @app.delete("/api/steps/{sid}", dependencies=auth)
    async def delete_step(sid: int):
        store.delete_step(sid)
        return {"ok": True}

    # --- check-in settings ---------------------------------------------------

    @app.get("/api/checkins", dependencies=auth)
    async def get_checkins():
        cfg = CheckinConfig.load(store, settings)
        return {**cfg.__dict__, "modes": list(MODES)}

    @app.put("/api/checkins", dependencies=auth)
    async def put_checkins(body: CheckinBody):
        cfg = CheckinConfig(**body.model_dump() if hasattr(body, "model_dump") else body.dict())
        try:
            cfg.save(store)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {**cfg.__dict__, "modes": list(MODES)}

    return app
