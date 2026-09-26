import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from assistant import deploy
from assistant.agent import Agent
from assistant.config import Settings
from assistant.db import Store
from assistant.deploy import Deployer, verify
from assistant.scheduler import Notifier
from assistant.web import create_app

SECRET = "s3cret"


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "current_branch", lambda: "main")
    settings = Settings(web_password="pw", secret_key="k" * 32, db_path=tmp_path / "t.db", deploy_secret=SECRET)
    script = tmp_path / "update.sh"
    script.write_text("echo pulled; exit ${FAIL:-0}\n")
    restarted = []
    deployer = Deployer(settings, script=script, on_success=lambda: restarted.append(True))
    store = Store(settings.db_path)
    app = create_app(settings, store, Agent(settings, store), Notifier(), deployer)
    with TestClient(app) as client:  # one event loop for the whole test, like uvicorn
        yield settings, client, deployer, restarted


def wait_for(cond, timeout=5):
    end = time.time() + timeout
    while time.time() < end and not cond():
        time.sleep(0.05)
    return cond()


def test_verify():
    body = b'{"a":1}'
    assert verify(SECRET, body, {"x-hub-signature-256": sign(body)})
    assert not verify(SECRET, body, {"x-hub-signature-256": sign(b"other")})
    assert verify(SECRET, b"", {"authorization": f"Bearer {SECRET}"})
    assert not verify(SECRET, b"", {"authorization": "Bearer nope"})
    assert not verify("", b"", {"authorization": "Bearer "})


def test_manual_deploy_runs_update_and_restarts(setup):
    settings, c, deployer, restarted = setup
    assert c.post("/hooks/deploy").status_code == 401
    assert c.post("/hooks/deploy", headers={"Authorization": "Bearer wrong"}).status_code == 401
    r = c.post("/hooks/deploy", headers={"Authorization": f"Bearer {SECRET}"})
    assert r.status_code == 202
    assert wait_for(lambda: restarted)
    assert deployer.last["ok"] and "pulled" in deployer.last["output"]
    status = c.get("/hooks/deploy", headers={"Authorization": f"Bearer {SECRET}"}).json()
    assert status["branch"] == "main" and status["last"]["reason"] == "manual"


def test_failed_update_does_not_restart(setup, monkeypatch):
    settings, c, deployer, restarted = setup
    monkeypatch.setenv("FAIL", "1")
    c.post("/hooks/deploy", headers={"Authorization": f"Bearer {SECRET}"})
    assert wait_for(lambda: deployer.last)
    assert deployer.last["ok"] is False and restarted == []
    assert (settings.db_path.parent / "deploy.log").read_text().count("FAILED") == 1


def test_github_events(setup):
    settings, c, deployer, restarted = setup

    def gh(event, payload):
        body = json.dumps(payload).encode()
        return c.post("/hooks/deploy", content=body,
                      headers={"X-GitHub-Event": event, "X-Hub-Signature-256": sign(body)})

    assert "pong" in gh("ping", {"zen": "hi"}).json()["message"]
    assert "ignored push" in gh("push", {"ref": "refs/heads/other"}).json()["message"]
    assert "ignored issues" in gh("issues", {}).json()["message"]
    assert restarted == []
    assert gh("push", {"ref": "refs/heads/main"}).status_code == 202
    assert wait_for(lambda: restarted)
    assert deployer.last["reason"] == "github push to main"


def test_deploy_port_serves_only_hooks(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "current_branch", lambda: "main")
    # TestClient requests arrive on port 80, so pretend that's the deploy port.
    settings = Settings(web_password="pw", secret_key="k" * 32, db_path=tmp_path / "t.db",
                        deploy_secret=SECRET, deploy_port=80)
    store = Store(settings.db_path)
    c = TestClient(create_app(settings, store, Agent(settings, store), Notifier()))
    assert c.get("/").status_code == 404
    assert c.post("/api/login", json={"password": "pw"}).status_code == 404
    assert c.get("/hooks/health").json() == {"ok": True}


def test_webhook_off_without_secret(tmp_path):
    settings = Settings(web_password="pw", secret_key="k" * 32, db_path=tmp_path / "t.db")
    store = Store(settings.db_path)
    c = TestClient(create_app(settings, store, Agent(settings, store), Notifier()))
    assert c.post("/hooks/deploy", headers={"Authorization": "Bearer "}).status_code == 404
