"""Provider-agnostic LLM gateway (offline deterministic default, OpenAI-compat, Ollama) with daily budget accounting."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.core.config import Settings
from backend.app.core.ssrf import validate_external_url
from backend.app.models.tables import LlmUsageEvent


class BudgetExceededError(RuntimeError):
    """Raised when the configured daily LLM budget has been reached."""


@dataclass(frozen=True, slots=True)
class LlmCompletionResult:
    """Result from an LLM call or deterministic offline fallback."""

    text: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    estimated_cost_usd: float
    used_offline_fallback: bool


class LlmGateway:
    """Provider-agnostic LLM wrapper with strict cost ledger and offline mode."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def today_spend_usd(self, db: Session) -> float:
        day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        total = db.scalar(
            select(func.coalesce(func.sum(LlmUsageEvent.estimated_cost_usd), 0.0)).where(
                LlmUsageEvent.created_at >= day_start
            )
        )
        return float(total or 0.0)

    def remaining_budget_usd(self, db: Session) -> float:
        return max(0.0, self.settings.llm_daily_budget_usd - self.today_spend_usd(db))

    def _record_usage(
        self,
        db: Session,
        *,
        operation: str,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        estimated_cost_usd: float,
    ) -> None:
        db.add(
            LlmUsageEvent(
                operation=operation,
                provider=provider,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                estimated_cost_usd=estimated_cost_usd,
            )
        )
        db.flush()

    async def complete_text(
        self,
        db: Session,
        *,
        operation: str,
        system_prompt: str,
        user_prompt: str,
        offline_response: str,
        max_tokens: int = 500,
    ) -> LlmCompletionResult:
        """Execute LLM completion if enabled & within budget; otherwise return deterministic offline output."""
        provider = self.settings.llm_provider.strip().lower()
        if provider in {"", "offline", "none"}:
            self._record_usage(
                db,
                operation=operation,
                provider="offline",
                model="deterministic-local",
                prompt_tokens=0,
                completion_tokens=0,
                estimated_cost_usd=0.0,
            )
            return LlmCompletionResult(
                text=offline_response,
                provider="offline",
                model="deterministic-local",
                prompt_tokens=0,
                completion_tokens=0,
                estimated_cost_usd=0.0,
                used_offline_fallback=True,
            )

        if self.remaining_budget_usd(db) <= 0.0:
            raise BudgetExceededError(
                f"Orçamento diário de LLM atingido (${self.settings.llm_daily_budget_usd:.2f})."
            )

        base_url = self.settings.llm_base_url.rstrip("/")
        # Allow local Ollama loopback explicitly when llm_provider == ollama/openai_compat on localhost
        allow_origins: set[str] = set()
        if "127.0.0.1" in base_url or "localhost" in base_url:
            from backend.app.core.ssrf import extract_origin

            allow_origins.add(extract_origin(base_url))

        endpoint = validate_external_url(
            f"{base_url}/chat/completions",
            allow_test_origins=allow_origins,
        )
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"

        payload = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": max_tokens,
        }

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(endpoint, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

        choices = data.get("choices") or []
        content = ""
        if choices:
            content = str((choices[0].get("message") or {}).get("content") or "").strip()
        usage = data.get("usage") or {}
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        # Conservative local estimate unless local Ollama ($0)
        cost = 0.0 if provider == "ollama" or allow_origins else round((pt * 0.000001) + (ct * 0.000003), 6)

        self._record_usage(
            db,
            operation=operation,
            provider=provider,
            model=self.settings.llm_model,
            prompt_tokens=pt,
            completion_tokens=ct,
            estimated_cost_usd=cost,
        )
        return LlmCompletionResult(
            text=content or offline_response,
            provider=provider,
            model=self.settings.llm_model,
            prompt_tokens=pt,
            completion_tokens=ct,
            estimated_cost_usd=cost,
            used_offline_fallback=not bool(content),
        )
