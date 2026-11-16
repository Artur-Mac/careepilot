"""Provider-agnostic LLM gateway (offline, Qwen local, OpenAI-compatible) with structured outputs."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.core.config import Settings, is_local_llm_url
from backend.app.core.ssrf import extract_origin, validate_external_url
from backend.app.models.tables import LlmUsageEvent
from backend.app.security.redaction import redact_secrets

T = TypeVar("T", bound=BaseModel)


class BudgetExceededError(RuntimeError):
    """Raised when the configured daily remote LLM budget has been reached."""


class RemoteProfileBlockedError(PermissionError):
    """Raised when sending candidate profile data to a remote LLM without explicit opt-in."""

    def __init__(self, destination: str) -> None:
        self.destination = destination
        super().__init__(
            f"Envio de dados do perfil para provedor remoto ({destination}) bloqueado. "
            "Habilite LLM_ALLOW_REMOTE_PROFILE=true apenas se desejar enviar dados para fora do host local."
        )


class LlmProviderError(RuntimeError):
    """Raised when a configured LLM provider fails or returns invalid structured output."""


@dataclass(frozen=True, slots=True)
class LlmCompletionResult:
    """Result from a text LLM call or deterministic offline fallback."""

    text: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    estimated_cost_usd: float
    used_offline_fallback: bool


@dataclass(frozen=True, slots=True)
class LlmStructuredResult[T: BaseModel]:
    """Validated structured result from LLM or deterministic offline builder."""

    data: T
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    estimated_cost_usd: float
    mode: str  # "llm_structured" | "offline_deterministic"


def _extract_json_object_text(raw: str) -> str:
    """Strip optional markdown code fences while requiring raw JSON object/array content."""
    text = raw.strip()
    fence = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text, re.I)
    if fence:
        return fence.group(1).strip()
    return text


_ENGINE_FALLBACK_TTL_SECONDS = 15.0
_ENGINE_TELEMETRY: dict[tuple[str, str, str], tuple[str, float]] = {}


def _engine_key(settings: Settings) -> tuple[str, str, str]:
    return (
        settings.llm_provider.strip().lower(),
        settings.llm_base_url.strip(),
        settings.llm_model.strip(),
    )


def record_engine_fallback(settings: Settings, reason_code: str) -> None:
    """Record a sanitized recent fallback reason code for /api/system/status."""
    import time

    safe_code = re.sub(r"[^a-z0-9_]", "_", reason_code.strip().lower())[:64] or "provider_fallback"
    _ENGINE_TELEMETRY[_engine_key(settings)] = (safe_code, time.monotonic() + _ENGINE_FALLBACK_TTL_SECONDS)


def clear_engine_fallback(settings: Settings) -> None:
    """Clear recent fallback state after a healthy provider completion."""
    _ENGINE_TELEMETRY.pop(_engine_key(settings), None)


def get_engine_telemetry(settings: Settings) -> dict[str, Any]:
    """Return effective execution engine mode, availability, and sanitized recent fallback reason."""
    import time

    provider = settings.llm_provider.strip().lower()
    base_url = settings.llm_base_url.strip()
    if provider in {"", "offline", "none"}:
        return {"mode": "offline_deterministic", "available": True, "fallback_reason": None}

    if provider in {"qwen_local", "openai_compat"}:
        if is_local_llm_url(base_url):
            mode = "local_llm"
        elif settings.llm_allow_remote_profile and provider == "openai_compat":
            mode = "cloud_assisted"
        else:
            return {
                "mode": "offline_deterministic",
                "available": True,
                "fallback_reason": "remote_profile_disabled",
            }
        entry = _ENGINE_TELEMETRY.get(_engine_key(settings))
        if entry is not None:
            reason, until = entry
            if time.monotonic() < until:
                return {"mode": mode, "available": False, "fallback_reason": reason}
            _ENGINE_TELEMETRY.pop(_engine_key(settings), None)
        return {"mode": mode, "available": bool(base_url), "fallback_reason": None if base_url else "missing_base_url"}

    return {"mode": "offline_deterministic", "available": True, "fallback_reason": "unsupported_provider"}


class LlmGateway:
    """Tool-free LLM client supporting Qwen local and OpenAI-compatible chat completions."""

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

    def record_usage(
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
        self._record_usage(
            db,
            operation=operation,
            provider=provider,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            estimated_cost_usd=estimated_cost_usd,
        )

    def _resolve_live_endpoint(
        self,
        db: Session | None,
        *,
        includes_candidate_profile: bool,
        remaining_budget_override: float | None = None,
    ) -> tuple[str, str, bool]:
        provider = self.settings.llm_provider.strip().lower()
        if provider not in {"qwen_local", "openai_compat"}:
            raise LlmProviderError(f"Provedor LLM não suportado: {provider!r}")

        base_url = self.settings.llm_base_url.strip().rstrip("/")
        if not base_url:
            raise LlmProviderError("LLM_BASE_URL não configurada para o provedor ativo.")

        is_local = is_local_llm_url(base_url)
        if provider == "qwen_local" and not is_local:
            raise LlmProviderError("O servidor Qwen local deve usar um endereço local (loopback).")
        dest_host = urlparse(base_url).hostname or base_url

        if includes_candidate_profile and not is_local and not self.settings.llm_allow_remote_profile:
            raise RemoteProfileBlockedError(dest_host)

        # Apply optional paid-provider daily budget only to remote endpoints.
        if not is_local:
            remaining = (
                remaining_budget_override
                if remaining_budget_override is not None
                else (self.remaining_budget_usd(db) if db is not None else self.settings.llm_daily_budget_usd)
            )
            if remaining <= 0.0:
                raise BudgetExceededError(
                    f"Orçamento diário de LLM atingido (${self.settings.llm_daily_budget_usd:.2f}) "
                    f"para provedor remoto ({dest_host})."
                )

        target_base = base_url
        if provider == "qwen_local" and (urlparse(target_base).path or "").strip("/") == "":
            target_base = f"{target_base}/v1"

        allow_local = {extract_origin(target_base)} if is_local else None
        endpoint = validate_external_url(
            f"{target_base}/chat/completions",
            allow_local_origins=allow_local,
        )
        return provider, endpoint, is_local

    async def _post_chat_completion(
        self,
        *,
        endpoint: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        response_format: dict[str, Any] | None = None,
    ) -> tuple[str, int, int]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"

        payload: dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": max_tokens,
        }
        if response_format is not None:
            payload["response_format"] = response_format

        try:
            async with httpx.AsyncClient(
                timeout=self.settings.llm_timeout_seconds,
                follow_redirects=False,
            ) as client:
                resp = await client.post(endpoint, headers=headers, json=payload)
                if 300 <= resp.status_code < 400:
                    record_engine_fallback(self.settings, "provider_redirect")
                    raise LlmProviderError(
                        f"Redirecionamento HTTP ({resp.status_code}) recusado no endpoint LLM."
                    )
                if resp.status_code in {400, 422} and response_format is not None:
                    body_low = resp.text.lower()
                    if "response_format" in body_low or "json_schema" in body_low or "schema" in body_low:
                        fallback_format = (
                            {"type": "json_object"}
                            if response_format.get("type") == "json_schema"
                            else None
                        )
                        retry_payload = {k: v for k, v in payload.items() if k != "response_format"}
                        if fallback_format is not None:
                            retry_payload["response_format"] = fallback_format
                        resp = await client.post(endpoint, headers=headers, json=retry_payload)
                if resp.status_code >= 400:
                    record_engine_fallback(self.settings, f"provider_http_{resp.status_code}")
                    detail = redact_secrets(resp.text[:240], self.settings.secret_values())
                    raise LlmProviderError(f"Falha HTTP {resp.status_code} no provedor LLM: {detail}")
                data = resp.json()
        except httpx.TimeoutException as exc:
            record_engine_fallback(self.settings, "provider_timeout")
            raise LlmProviderError("Tempo esgotado na comunicação com o provedor LLM.") from exc
        except httpx.HTTPError as exc:
            record_engine_fallback(self.settings, "provider_unreachable")
            raise LlmProviderError("Provedor LLM indisponível ou inacessível.") from exc

        clear_engine_fallback(self.settings)
        choices = data.get("choices") or []
        content = ""
        if choices:
            content = str((choices[0].get("message") or {}).get("content") or "").strip()
        usage = data.get("usage") or {}
        pt = int(usage.get("prompt_tokens") or 0)
        ct = int(usage.get("completion_tokens") or 0)
        return content, pt, ct

    async def complete_text(
        self,
        db: Session | None,
        *,
        operation: str,
        system_prompt: str,
        user_prompt: str,
        offline_response: str,
        max_tokens: int = 500,
        includes_candidate_profile: bool = False,
        remaining_budget_override: float | None = None,
    ) -> LlmCompletionResult:
        """Execute text LLM completion if configured; otherwise return deterministic offline output."""
        provider = self.settings.llm_provider.strip().lower()
        if provider in {"", "offline", "none"}:
            if db is not None:
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

        provider, endpoint, is_local = self._resolve_live_endpoint(
            db,
            includes_candidate_profile=includes_candidate_profile,
            remaining_budget_override=remaining_budget_override,
        )
        content, pt, ct = await self._post_chat_completion(
            endpoint=endpoint,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=max_tokens,
        )
        cost = 0.0 if is_local else round((pt * 0.000001) + (ct * 0.000003), 6)
        if db is not None:
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

    async def complete_structured(
        self,
        db: Session | None,
        *,
        operation: str,
        system_prompt: str,
        user_payload: dict[str, Any],
        output_model: type[T],
        validate_ids: Callable[[T], None] | None = None,
        offline_factory: Callable[[], T] | None = None,
        includes_candidate_profile: bool = False,
        remaining_budget_override: float | None = None,
        max_tokens: int = 1200,
    ) -> LlmStructuredResult[T]:
        """Request strict JSON structured output validated by Pydantic with one corrective retry."""
        provider = self.settings.llm_provider.strip().lower()
        if provider in {"", "offline", "none"}:
            if offline_factory is None:
                raise LlmProviderError(
                    f"Operação estruturada '{operation}' indisponível no modo offline."
                )
            data = offline_factory()
            if validate_ids is not None:
                validate_ids(data)
            if db is not None:
                self._record_usage(
                    db,
                    operation=operation,
                    provider="offline",
                    model="deterministic-local",
                    prompt_tokens=0,
                    completion_tokens=0,
                    estimated_cost_usd=0.0,
                )
            return LlmStructuredResult(
                data=data,
                provider="offline",
                model="deterministic-local",
                prompt_tokens=0,
                completion_tokens=0,
                estimated_cost_usd=0.0,
                mode="offline_deterministic",
            )

        provider, endpoint, is_local = self._resolve_live_endpoint(
            db,
            includes_candidate_profile=includes_candidate_profile,
            remaining_budget_override=remaining_budget_override,
        )
        schema = output_model.model_json_schema()
        schema_str = json.dumps(schema, ensure_ascii=False)
        full_system = (
            f"{system_prompt}\n\n"
            "Responda EXCLUSIVAMENTE com um único objeto JSON válido compatível com este JSON Schema. "
            "Sem texto fora do JSON, sem chaves extras, sem ferramentas e usando apenas os IDs fornecidos.\n"
            f"Schema: {schema_str}"
        )
        user_content = json.dumps(user_payload, ensure_ascii=False)
        messages: list[dict[str, str]] = [
            {"role": "system", "content": full_system},
            {"role": "user", "content": user_content},
        ]
        response_format = {
            "type": "json_schema",
            "json_schema": {
                "name": output_model.__name__,
                "strict": True,
                "schema": schema,
            },
        }

        total_pt = 0
        total_ct = 0
        last_err: Exception | None = None

        for attempt in range(2):
            try:
                raw_text, pt, ct = await self._post_chat_completion(
                    endpoint=endpoint,
                    messages=messages,
                    max_tokens=max_tokens,
                    response_format=response_format,
                )
            except (httpx.HTTPError, LlmProviderError) as exc:
                raise LlmProviderError(
                    f"Falha ao consultar provedor LLM ({provider}/{self.settings.llm_model}): {exc}"
                ) from exc

            total_pt += pt
            total_ct += ct
            cleaned = _extract_json_object_text(raw_text)
            try:
                parsed = output_model.model_validate_json(cleaned)
                if validate_ids is not None:
                    validate_ids(parsed)
                cost = 0.0 if is_local else round((total_pt * 0.000001) + (total_ct * 0.000003), 6)
                if db is not None:
                    self._record_usage(
                        db,
                        operation=operation,
                        provider=provider,
                        model=self.settings.llm_model,
                        prompt_tokens=total_pt,
                        completion_tokens=total_ct,
                        estimated_cost_usd=cost,
                    )
                return LlmStructuredResult(
                    data=parsed,
                    provider=provider,
                    model=self.settings.llm_model,
                    prompt_tokens=total_pt,
                    completion_tokens=total_ct,
                    estimated_cost_usd=cost,
                    mode="llm_structured",
                )
            except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                last_err = exc
                if attempt == 0:
                    err_msg = redact_secrets(str(exc), self.settings.secret_values())[:600]
                    messages.append({"role": "assistant", "content": raw_text[:2000]})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "A resposta anterior falhou na validação estruturada: "
                                f"{err_msg}. Corrija e retorne APENAS JSON válido seguindo o schema "
                                "e exclusivamente IDs existentes na entrada."
                            ),
                        }
                    )

        cost = 0.0 if is_local else round((total_pt * 0.000001) + (total_ct * 0.000003), 6)
        if db is not None:
            self._record_usage(
                db,
                operation=f"{operation}:failed",
                provider=provider,
                model=self.settings.llm_model,
                prompt_tokens=total_pt,
                completion_tokens=total_ct,
                estimated_cost_usd=cost,
            )
        raise LlmProviderError(
            f"O provedor LLM ({self.settings.llm_model}) retornou estrutura inválida após 2 tentativas: {last_err}"
        )
