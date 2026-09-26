"""Rebuild webhook: POST /hooks/deploy pulls the latest code and restarts the service.

Accepts either a GitHub push webhook (signed with DEPLOY_SECRET) or a manual call with
`Authorization: Bearer <DEPLOY_SECRET>`. The update runs deploy/update.sh; if it
succeeds, the process exits and systemd (Restart=always) starts the new code.

These routes are also served on their own port (DEPLOY_PORT), which serves nothing
else, so that port can be exposed to GitHub without exposing the web UI.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import signal
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from fastapi import APIRouter, HTTPException, Request

from .config import ROOT, Settings

log = logging.getLogger(__name__)

UPDATE_SCRIPT = ROOT / "deploy" / "update.sh"


def verify(secret: str, body: bytes, headers) -> bool:
    if not secret:
        return False
    sig = headers.get("x-hub-signature-256", "")
    if sig.startswith("sha256="):
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)
    auth = headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else headers.get("x-deploy-token", "")
    return bool(token) and hmac.compare_digest(token.encode(), secret.encode())


def current_branch() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=10
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def restart_self() -> None:
    """Exit cleanly; systemd's Restart=always brings the service back on the new code."""
    log.info("restarting to load the update")
    os.kill(os.getpid(), signal.SIGTERM)


class Deployer:
    def __init__(self, settings: Settings, log_path: Optional[Path] = None,
                 script: Path = UPDATE_SCRIPT, on_success: Callable[[], None] = restart_self):
        self.settings = settings
        self.log_path = log_path or settings.db_path.parent / "deploy.log"
        self.script = script
        self.on_success = on_success
        self.running = False
        self.task: Optional[asyncio.Task] = None
        self.last: dict = {}

    async def run(self, reason: str) -> None:
        self.running = True
        started = datetime.now(self.settings.tz)
        try:
            proc = await asyncio.create_subprocess_exec(
                "bash", str(self.script), cwd=ROOT,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
            output, ok = out.decode(errors="replace"), proc.returncode == 0
        except (OSError, asyncio.TimeoutError) as e:
            output, ok = f"update failed to run: {e}", False
        self.last = {"at": started.isoformat(timespec="seconds"), "reason": reason, "ok": ok, "output": output[-4000:]}
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as f:
            f.write(f"\n=== {started:%Y-%m-%d %H:%M:%S} {reason} -> {'OK' if ok else 'FAILED'}\n{output}")
        self.running = False
        if ok:
            log.info("update succeeded (%s)", reason)
            await asyncio.sleep(1)  # let the webhook response finish
            self.on_success()
        else:
            log.error("update failed (%s); still running the old code. See %s", reason, self.log_path)


def create_hook_router(settings: Settings, deployer: Deployer) -> APIRouter:
    router = APIRouter(prefix="/hooks")

    @router.get("/health")
    async def health():
        return {"ok": True}

    @router.get("/deploy")
    async def status(request: Request):
        if not verify(settings.deploy_secret, b"", request.headers):
            raise HTTPException(401, "bad or missing deploy secret")
        return {"running": deployer.running, "branch": current_branch(), "last": deployer.last}

    @router.post("/deploy", status_code=202)
    async def deploy(request: Request):
        if not settings.deploy_secret:
            raise HTTPException(404, "deploy webhook is off (set DEPLOY_SECRET in .env)")
        body = await request.body()
        if not verify(settings.deploy_secret, body, request.headers):
            raise HTTPException(401, "bad or missing deploy secret")

        event = request.headers.get("x-github-event", "")
        reason = "manual"
        if event == "ping":
            return {"ok": True, "message": "pong - webhook is set up"}
        if event == "push":
            try:
                ref = json.loads(body or b"{}").get("ref", "")
            except json.JSONDecodeError:
                raise HTTPException(400, "bad JSON")
            branch = current_branch()
            if ref != f"refs/heads/{branch}":
                return {"ok": True, "message": f"ignored push to {ref}; running {branch}"}
            reason = f"github push to {branch}"
        elif event:
            return {"ok": True, "message": f"ignored {event} event"}

        if deployer.running:
            raise HTTPException(409, "an update is already running")
        deployer.running = True
        deployer.task = asyncio.get_running_loop().create_task(deployer.run(reason))  # keep a reference
        return {"ok": True, "message": "updating - the service will restart in a few seconds"}

    return router
