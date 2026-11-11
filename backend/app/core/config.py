"""Application configuration loaded from environment variables with safe local defaults."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings for CareerPilot MVP.

    Defaults enforce DRY_RUN=True, loopback-only binding, and offline-capable execution.
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

    # Safety switches — DRY_RUN is True by default and live mode is separately gated.
    dry_run: bool = True
    live_mode_enabled: bool = False
    test_ats_enabled: bool = True
    test_ats_url: str = "http://127.0.0.1:8010"

    # Single-user local dashboard unlock & session signing
    careerpilot_passcode: str = "demo-local-2026"
    careerpilot_session_secret: str = "careerpilot-local-session-secret-dev-only"
    session_ttl_seconds: int = 60 * 60 * 12

    # SQLite WAL + local artifact storage
    database_url: str = "sqlite:///./data/careerpilot.db"
    artifacts_dir: str = "./data/artifacts"

    # Provider-agnostic LLM (OpenAI-compatible or Ollama); defaults to deterministic offline mode
    llm_provider: str = "offline"
    llm_base_url: str = "http://127.0.0.1:11434/v1"
    llm_model: str = "qwen2.5:7b-instruct"
    llm_api_key: str = ""
    llm_daily_budget_usd: float = 1.0
    feature_visual_fallback: bool = False

    # Optional external API keys (connectors stay fixture/disabled without real keys)
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

    # Background scheduler (6-12h default interval)
    scheduler_enabled: bool = False
    scheduler_interval_hours: int = Field(default=8, ge=1, le=24)

    # Browser automation runs without a window unless the operator opts in.
    playwright_chromium_path: str = ""
    playwright_headless: bool = True

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
    def test_ats_origin(self) -> str | None:
        if not self.test_ats_enabled or not self.test_ats_url:
            return None
        parsed = urlparse(self.test_ats_url)
        if parsed.scheme in {"http", "https"} and parsed.hostname in {"127.0.0.1", "localhost"}:
            port = f":{parsed.port}" if parsed.port else ""
            return f"{parsed.scheme}://{parsed.hostname}{port}"
        return None

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
