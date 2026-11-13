"""Shared base classes, blocker detectors, and HTML helpers for logged-in browser collectors."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime

from backend.app.browser_automation.session import BrowserSession
from backend.app.connectors.base import infer_seniority, infer_workplace_type
from backend.app.core.ssrf import validate_external_url
from backend.app.schemas.contracts import NormalizedJob
from backend.app.security.prompt_guard import sanitize_untrusted_text


class CollectorBlockedError(RuntimeError):
    """Raised when a portal presents a login wall, CAPTCHA, or anti-bot gate."""


@dataclass(frozen=True, slots=True)
class BrowserDiscoveryPlan:
    """Profile-driven parameters passed into browser collectors."""

    roles: list[str]
    location: str | None = "Recife, PE"
    remote_only: bool = False
    include_remote: bool = True
    limit_per_source: int = 15
    base_url_override: str | None = None


_CAPTCHA_SIGNATURES: tuple[str, ...] = (
    "/sorry/index",
    "/cdn-cgi/challenge-platform",
    "unusual traffic from your computer",
    "tráfego incomum",
    "verify you are human",
    "confirme que você é humano",
    "g-recaptcha",
    "h-captcha",
    "cf-turnstile",
)


def detect_portal_blocker(html: str, page_url: str, *, portal: str) -> str | None:
    """Detect prominent login walls, CAPTCHAs, or anti-bot gates conservatively."""
    low_url = (page_url or "").lower()
    low_html = (html or "")[:20000].lower()

    for sig in _CAPTCHA_SIGNATURES:
        if sig in low_url or sig in low_html:
            return (
                f"{portal} apresentou verificação anti-bot/CAPTCHA no Chrome. "
                "Resolva manualmente na janela do navegador e repita a busca."
            )

    if portal.lower() == "linkedin":
        if any(
            marker in low_url
            for marker in ("/login", "/uas/login", "/authwall", "/checkpoint/challenge")
        ):
            return (
                "LinkedIn exige sessão autenticada no Chrome. "
                "Faça login na janela aberta por ./scripts/start_browser.sh."
            )
        if 'action="/uas/login' in low_html or "authwall-join-form" in low_html:
            return (
                "LinkedIn exibiu bloqueio de autenticação (authwall). "
                "Entre na sua conta no Chrome conectado."
            )

    if any(marker in low_url for marker in ("/login", "/signin", "/entrar")) and not re.search(
        r"(vaga|job|emprego|search)", low_url
    ):
        return f"{portal} redirecionou para página de login no Chrome."
    return None


def make_browser_job(
    *,
    source: str,
    source_job_id: str | None,
    company: str,
    title: str,
    location: str | None,
    description: str,
    job_url: str,
    apply_url: str | None = None,
    attribution: str,
    workplace_hint: str = "",
    allow_test_origins: set[str] | None = None,
) -> NormalizedJob:
    """Sanitize fields, validate URL safely, and construct NormalizedJob."""
    safe_job_url = validate_external_url(job_url, allow_test_origins=allow_test_origins)
    safe_apply_url = (
        validate_external_url(apply_url, allow_test_origins=allow_test_origins)
        if apply_url
        else safe_job_url
    )
    clean_title = sanitize_untrusted_text(title).text[:240] or "Vaga sem título"
    clean_company = sanitize_untrusted_text(company).text[:200] or "Empresa não informada"
    clean_loc = sanitize_untrusted_text(location).text[:200] if location else None
    clean_desc = sanitize_untrusted_text(description).text[:48000] or clean_title
    wp = infer_workplace_type(f"{clean_title} {clean_loc or ''} {workplace_hint} {clean_desc[:600]}")

    payload = {
        "source": source,
        "source_job_id": (source_job_id or safe_job_url)[:256],
        "company": clean_company,
        "title": clean_title,
        "location": clean_loc,
        "workplace_type": wp,
        "description": clean_desc,
        "job_url": safe_job_url,
        "apply_url": safe_apply_url,
        "posted_at": None,
        "discovered_at": datetime.now(UTC),
        "employment_type": None,
        "seniority": infer_seniority(clean_title),
        "salary_min": None,
        "salary_max": None,
        "salary_currency": None,
        "source_attribution": attribution[:300],
        "discovery_channel": "browser",
    }
    ctx = {"allow_test_origins": allow_test_origins} if allow_test_origins else None
    return NormalizedJob.model_validate(payload, context=ctx)


class BrowserCollector(ABC):
    """Protocol for CDP browser job collectors with error isolation."""

    name: str
    kind: str = "browser"

    @abstractmethod
    async def collect(
        self,
        session: BrowserSession,
        plan: BrowserDiscoveryPlan,
    ) -> list[NormalizedJob]:
        """Collect job postings using a bounded discovery tab in BrowserSession."""
