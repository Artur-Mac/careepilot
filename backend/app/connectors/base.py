"""Base JobSource interface, resilient HTTP fetch with backoff/jitter, and normalization helpers."""

from __future__ import annotations

import asyncio
import random
import re
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any

import httpx

from backend.app.core.config import Settings
from backend.app.core.ssrf import validate_external_url
from backend.app.schemas.contracts import NormalizedJob, SearchQuery, WorkplaceType
from backend.app.security.prompt_guard import sanitize_untrusted_text


class ConnectorFetchError(RuntimeError):
    """Raised when an external source cannot be fetched after bounded retries."""


def infer_workplace_type(text: str, *, is_remote: bool | None = None) -> WorkplaceType:
    """Classify workplace type conservatively from structured flags or text."""
    if is_remote is True:
        return "remote"
    low = (text or "").lower()
    if any(k in low for k in ("hybrid", "híbrido", "hibrido")):
        return "hybrid"
    if any(k in low for k in ("remote", "remoto", "teletrabalho", "home office", "worldwide")):
        return "remote"
    if any(k in low for k in ("onsite", "on-site", "presencial")):
        return "onsite"
    return "unknown"


def infer_seniority(title: str, explicit: str | None = None) -> str | None:
    """Normalize seniority label from explicit tag or title keywords."""
    combined = f"{explicit or ''} {title}".lower()
    if any(k in combined for k in ("intern", "estágio", "estagio", "estagiári", "working student")):
        return "intern"
    if "trainee" in combined:
        return "trainee"
    if any(k in combined for k in ("junior", "júnior", "jr", "entry-level", "entry level", "graduate")):
        return "junior"
    if any(k in combined for k in ("staff", "principal", "lead", "head", "architect", "sênior", "senior", "sr.")):
        return "senior"
    if any(k in combined for k in ("pleno", "mid-level", "mid")):
        return "mid"
    return explicit.strip().lower() if explicit else None


def parse_timestamp(val: Any) -> datetime | None:
    """Parse ISO-8601 strings or Unix timestamps (seconds or milliseconds) into UTC datetime."""
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        ts = float(val)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=UTC)
    if isinstance(val, str):
        raw = val.strip()
        if raw.isdigit():
            return parse_timestamp(int(raw))
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def parse_salary_string(text: str | None) -> tuple[float | None, float | None, str | None]:
    """Extract numeric min/max/currency conservatively from free-text salary tags."""
    if not text:
        return None, None, None
    cur = "BRL" if ("R$" in text or "BRL" in text.upper()) else ("EUR" if "EUR" in text.upper() or "€" in text else ("USD" if "USD" in text.upper() or "$" in text else None))
    nums: list[float] = []
    for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*([kK]?)", text):
        raw = m.group(1).replace(",", ".")
        try:
            val = float(raw)
        except ValueError:
            continue
        if m.group(2).lower() == "k":
            val *= 1000.0
        nums.append(val)
    if not nums:
        return None, None, cur
    if len(nums) == 1:
        return nums[0], nums[0], cur
    return min(nums[0], nums[1]), max(nums[0], nums[1]), cur


class JobSource(ABC):
    """Frozen JobSource connector protocol with rate limits, error isolation, and SSRF safety."""

    name: str
    kind: str
    default_mode: str = "mocked"

    def __init__(
        self,
        settings: Settings,
        *,
        use_live_network: bool = False,
        http_client: httpx.AsyncClient | None = None,
        max_retries: int = 2,
        backoff_base_seconds: float = 0.05,
    ) -> None:
        self.settings = settings
        self.use_live_network = use_live_network
        self._http_client = http_client
        self.max_retries = max_retries
        self.backoff_base_seconds = backoff_base_seconds

    @abstractmethod
    async def discover(self, query: SearchQuery) -> list[NormalizedJob]:
        """Discover normalized jobs honoring query limits and provider terms."""

    def _clean_description(self, raw_html_or_text: str) -> str:
        return sanitize_untrusted_text(raw_html_or_text).text or "Descrição não informada."

    async def _get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Fetch JSON safely via HTTPS with SSRF validation, no unsafe redirects, and bounded backoff."""
        safe_url = validate_external_url(url, resolve_dns=self._http_client is None)
        own_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(12.0),
            follow_redirects=False,
            headers={"User-Agent": "CareerPilot-LocalAssistant/0.1 (+local-user)"},
        )
        try:
            last_error: Exception | None = None
            for attempt in range(self.max_retries + 1):
                try:
                    resp = await client.get(safe_url, params=params, headers=headers)
                    if resp.status_code == 429:
                        retry_after = min(float(resp.headers.get("Retry-After", "1")), 3.0)
                        if attempt < self.max_retries:
                            await asyncio.sleep(retry_after + random.uniform(0.01, 0.05))
                            continue
                        raise ConnectorFetchError(f"{self.name}: HTTP 429 rate limited")
                    if resp.status_code >= 500:
                        if attempt < self.max_retries:
                            delay = self.backoff_base_seconds * (2**attempt) + random.uniform(0.0, 0.03)
                            await asyncio.sleep(delay)
                            continue
                        raise ConnectorFetchError(f"{self.name}: HTTP {resp.status_code}")
                    if 300 <= resp.status_code < 400:
                        raise ConnectorFetchError(f"{self.name}: redirect bloqueado ({resp.status_code})")
                    resp.raise_for_status()
                    return resp.json()
                except (httpx.HTTPError, ValueError) as exc:
                    last_error = exc
                    if attempt < self.max_retries and isinstance(exc, httpx.TransportError):
                        await asyncio.sleep(self.backoff_base_seconds * (2**attempt))
                        continue
                    break
            raise ConnectorFetchError(f"{self.name}: falha ao consultar fonte ({last_error})")
        finally:
            if own_client:
                await client.aclose()
