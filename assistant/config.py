"""Settings, loaded from the .env file in the project root (see .env.example)."""
from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger(__name__)


def _bool(value: Optional[str], default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    assistant_name: str = "Juno"
    user_name: str = ""
    timezone: str = "America/New_York"

    ollama_url: str = "http://127.0.0.1:11434"
    model: str = "qwen3:4b"
    num_ctx: int = 8192
    think: bool = False
    history_messages: int = 16

    web_host: str = "0.0.0.0"
    web_port: int = 8080
    web_password: str = ""
    secret_key: str = ""

    discord_token: str = ""
    discord_owner_id: Optional[int] = None

    # Proactive check-ins. These are defaults; the web UI can override them (stored in the DB).
    coach_mode: str = "coach"  # off | light | balanced | coach
    morning_time: str = "08:00"
    midday_time: str = "13:00"
    evening_time: str = "20:30"
    quiet_hours: str = "22:00-07:30"

    weather_location: str = ""  # e.g. "Boston, MA"; used for the morning brief and the weather tool
    searxng_url: str = ""  # optional self-hosted search; DuckDuckGo is used otherwise

    db_path: Path = ROOT / "data" / "assistant.db"
    persona_path: Path = ROOT / "persona.md"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def persona(self) -> str:
        try:
            return self.persona_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""


def load_settings(env_file: Optional[Path] = None) -> Settings:
    load_dotenv(env_file or ROOT / ".env")
    env = os.environ.get

    owner = env("DISCORD_OWNER_ID", "").strip()
    s = Settings(
        assistant_name=env("ASSISTANT_NAME", "Juno").strip() or "Juno",
        user_name=env("USER_NAME", "").strip(),
        timezone=env("TIMEZONE", "America/New_York").strip(),
        ollama_url=env("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/"),
        model=env("MODEL", "qwen3:4b").strip(),
        num_ctx=int(env("NUM_CTX", "8192")),
        think=_bool(env("THINK"), False),
        history_messages=int(env("HISTORY_MESSAGES", "16")),
        web_host=env("WEB_HOST", "0.0.0.0"),
        web_port=int(env("WEB_PORT", "8080")),
        web_password=env("WEB_PASSWORD", ""),
        secret_key=env("SECRET_KEY", ""),
        discord_token=env("DISCORD_TOKEN", "").strip(),
        discord_owner_id=int(owner) if owner.isdigit() else None,
        coach_mode=env("COACH_MODE", "coach").strip().lower() or "coach",
        morning_time=env("MORNING_TIME", "08:00").strip(),
        midday_time=env("MIDDAY_TIME", "13:00").strip(),
        evening_time=env("EVENING_TIME", "20:30").strip(),
        quiet_hours=env("QUIET_HOURS", "22:00-07:30").strip(),
        weather_location=env("WEATHER_LOCATION", "").strip(),
        searxng_url=env("SEARXNG_URL", "").strip().rstrip("/"),
        db_path=Path(env("DB_PATH", str(ROOT / "data" / "assistant.db"))),
        persona_path=Path(env("PERSONA_FILE", str(ROOT / "persona.md"))),
    )
    ZoneInfo(s.timezone)  # fail fast on a typo'd timezone

    if not s.secret_key:
        s.secret_key = secrets.token_hex(32)
        log.warning("SECRET_KEY is not set; web logins will reset on every restart.")
    if not s.web_password:
        log.warning("WEB_PASSWORD is not set; the web UI will refuse all logins.")
    if s.discord_token and s.discord_owner_id is None:
        log.warning("DISCORD_TOKEN is set but DISCORD_OWNER_ID is not; Discord bot disabled.")
    return s
