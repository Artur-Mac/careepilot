"""Shared base classes, blocker detectors, and HTML helpers for logged-in browser collectors."""

from __future__ import annotations

import asyncio
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from backend.app.browser_automation.session import BrowserSession
from backend.app.connectors.base import infer_seniority, infer_workplace_type
from backend.app.connectors.search.company_jsonld import extract_job_postings_from_jsonld
from backend.app.core.ssrf import UnsafeURLError, validate_external_url
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


_DETAIL_SELECTORS = {
    "linkedin": ".jobs-description__content, .show-more-less-html__markup, [itemprop='description']",
    "gupy": "[data-testid='job-description'], [itemprop='description']",
    "infojobs": ".js_job-description, .job-description, [itemprop='description']",
}


async def goto_with_transient_retry(
    session: BrowserSession,
    page: Page,
    url: str,
    *,
    timeout_ms: int = 12000,
) -> str:
    """Navigate with at most one retry for transient network failures, stopping immediately on unsafe origin."""
    try:
        return await session.safe_goto(page, url, timeout_ms=timeout_ms)
    except UnsafeURLError:
        raise
    except Exception:
        await asyncio.sleep(0.2 if session.allow_test_origins else 2.0)
        return await session.safe_goto(page, url, timeout_ms=timeout_ms)


async def enrich_job_details(
    page: Page, session: BrowserSession, jobs: list[NormalizedJob], *, limit: int = 4,
) -> None:
    """Read a bounded set of visible postings while preserving the original search cards."""
    for index, job in enumerate(jobs[:limit]):
        if index or not session.allow_test_origins:
            await asyncio.sleep(2)
        try:
            final_url = await goto_with_transient_retry(session, page, job.job_url, timeout_ms=6000)
            html = await page.content()
            if detect_portal_blocker(html, final_url, portal=job.source):
                break
            source_host = urlparse(job.job_url).hostname
            if urlparse(final_url).hostname != source_host:
                continue
            details = extract_job_postings_from_jsonld(
                html, final_url, source_name=job.source, allow_test_origins=session.allow_test_origins or None,
            )
            detail = next((item for item in details if item.title.casefold() == job.title.casefold()), None)
            if detail is not None and len(detail.description) >= len(job.description):
                changes = {"description": detail.description}
                for key in ("location", "employment_type", "seniority", "posted_at"):
                    value = getattr(detail, key)
                    if value:
                        changes[key] = value
                if detail.workplace_type != "unknown":
                    changes["workplace_type"] = detail.workplace_type
                jobs[index] = job.model_copy(update=changes)
                continue
            node = BeautifulSoup(html, "html.parser").select_one(_DETAIL_SELECTORS[job.source])
            if node is None:
                continue
            description = sanitize_untrusted_text(node.get_text("\n", strip=True)).text[:48000]
            if len(description) >= len(job.description):
                jobs[index] = job.model_copy(update={"description": description})

            # Preserve the discovered direct ATS link when a validated outgoing apply anchor exists.
            for link in node.find_all_next("a", href=True, limit=12):
                if (link.get("data-testid") or "") not in {"apply-button", "job-apply-button"}:
                    continue
                target = validate_external_url(
                    urljoin(final_url, str(link["href"])), allow_test_origins=session.allow_test_origins or None,
                )
                jobs[index] = jobs[index].model_copy(update={"apply_url": target})
                break
        except (PlaywrightError, UnsafeURLError, ValueError):
            continue


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
