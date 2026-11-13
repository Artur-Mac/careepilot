"""Application configuration loaded from environment variables with safe local defaults."""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def is_local_llm_url(base_url: str) -> bool:
    """Return True only when base_url parses cleanly to an exact loopback host."""
    raw = (base_url or "").strip()
    if not raw:
        return False
    try:
        parsed = urlparse(raw)
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False
    host = parsed.hostname.strip().rstrip(".").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class Settings(BaseSettings):
    """Runtime settings for CareerPilot.

    Defaults enforce loopback-only binding, local-first LLM usage, and explicit operator control.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    careerpilot_env: str = "development"
    careerpilot_host: str = "127.0.0.1"
    careerpilot_port: int = 8000

    # Single-user local dashboard unlock & session signing
    careerpilot_passcode: str = "demo-local-2026"
    careerpilot_session_secret: str = "careerpilot-local-session-secret-dev-only"
    session_ttl_seconds: int = 60 * 60 * 12

    # SQLite WAL + local artifact storage
    database_url: str = "sqlite:///./data/careerpilot.db"
    artifacts_dir: str = "./data/artifacts"

    # Provider-agnostic LLM (Ollama or OpenAI-compatible); defaults to deterministic offline mode
    llm_provider: str = "offline"
    llm_base_url: str = "http://127.0.0.1:11434/v1"
    llm_model: str = "qwen2.5:7b-instruct"
    llm_api_key: str = ""
    llm_daily_budget_usd: float = 1.0
    llm_allow_remote_profile: bool = False
    llm_timeout_seconds: float = Field(default=30.0, ge=1.0, le=300.0)
    llm_max_context_chars: int = Field(default=24000, ge=2000, le=200000)

    # Optional legacy external API keys (removed when browser discovery replaces them in Phase 2)
    greenhouse_boards: str = ""
    ashby_boards: str = ""
    lever_boards: str = ""
    brave_search_api_key: str = ""
    tavily_api_key: str = ""
    adzuna_app_id: str = ""
    adzuna_app_key: str = ""
    himalayas_api_key: str = ""

    # Notification channels
    notification_channel: str = "local_outbox"
    telegram_bot_token: str = ""
    telegram_allowed_chat_ids: str = ""

    # Optional Gmail desktop OAuth
    gmail_client_secrets_path: str = ""
    gmail_token_path: str = ""

    @field_validator("careerpilot_host")
    @classmethod
    def _enforce_loopback_host(cls, value: str) -> str:
        allowed = {"127.0.0.1", "localhost", "::1"}
        if value.strip().lower() not in allowed:
            raise ValueError(
                f"CAREERPILOT_HOST must remain bound to loopback ({sorted(allowed)}), got {value!r}"
            )
        return value.strip()

    @property
    def artifacts_path(self) -> Path:
        path = Path(self.artifacts_dir).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def allowed_origins(self) -> set[str]:
        origins = {
            f"http://127.0.0.1:{self.careerpilot_port}",
            f"http://localhost:{self.careerpilot_port}",
        }
        return origins

    @property
    def llm_is_local(self) -> bool:
        return self.llm_provider == "offline" or is_local_llm_url(self.llm_base_url)

    def secret_values(self) -> list[str]:
        """Return configured secret strings so log/evidence redactors can scrub them."""
        candidates = [
            self.careerpilot_passcode,
            self.careerpilot_session_secret,
            self.llm_api_key,
            self.brave_search_api_key,
            self.tavily_api_key,
            self.adzuna_app_key,
            self.himalayas_api_key,
            self.telegram_bot_token,
        ]
        return [c for c in candidates if c and len(c) >= 6]

    def masked_diagnostics(self) -> dict[str, dict[str, object]]:
        """Expose presence booleans only — never raw secrets."""
        fields = {
            "llm_api_key": self.llm_api_key,
            "brave_search_api_key": self.brave_search_api_key,
            "tavily_api_key": self.tavily_api_key,
            "adzuna_app_id": self.adzuna_app_id,
            "adzuna_app_key": self.adzuna_app_key,
            "himalayas_api_key": self.himalayas_api_key,
            "telegram_bot_token": self.telegram_bot_token,
            "gmail_client_secrets_path": self.gmail_client_secrets_path,
        }
        return {
            name: {
                "configured": bool(val.strip()),
                "preview": f"***{val[-4:]}" if len(val.strip()) >= 8 else ("***" if val.strip() else ""),
            }
            for name, val in fields.items()
        }


@lru_cache
def get_settings() -> Settings:
    """Return cached runtime settings."""
    return Settings()
