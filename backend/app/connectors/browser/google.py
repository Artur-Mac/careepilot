"""Google search + company career/JSON-LD page browser collector."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from urllib.parse import parse_qs, quote_plus, urlparse

from bs4 import BeautifulSoup

from backend.app.browser_automation.session import BrowserSession
from backend.app.connectors.browser.base import (
    BrowserCollector,
    BrowserDiscoveryPlan,
    CollectorBlockedError,
    detect_portal_blocker,
    make_browser_job,
)
from backend.app.connectors.search.company_jsonld import extract_job_postings_from_jsonld
from backend.app.connectors.search.query_generator import (
    QueryGenerationConfig,
    generate_search_queries,
)
from backend.app.core.ssrf import UnsafeURLError, validate_external_url
from backend.app.schemas.contracts import NormalizedJob

_GOOGLE_INTERNAL_HOSTS: frozenset[str] = frozenset(
    {
        "google.com",
        "www.google.com",
        "www.google.com.br",
        "google.com.br",
        "accounts.google.com",
        "support.google.com",
        "policies.google.com",
        "maps.google.com",
        "translate.google.com",
        "webcache.googleusercontent.com",
        "consent.google.com",
    }
)


def parse_google_results_html(
    html: str,
    page_url: str,
    *,
    allow_test_origins: set[str] | None = None,
    limit: int = 8,
) -> list[str]:
    """Extract and SSRF-validate external candidate career URLs from Google SERP HTML."""
    blocker = detect_portal_blocker(html, page_url, portal="Google")
    if blocker:
        raise CollectorBlockedError(blocker)

    soup = BeautifulSoup(html, "html.parser")
    candidates: list[str] = []
    seen: set[str] = set()

    for link in soup.select("#search a[href], #rso a[href], a[href]"):
        raw_href = str(link.get("href") or "").strip()
        if not raw_href or raw_href.startswith("#"):
            continue
        target = raw_href
        if raw_href.startswith("/url?"):
            qs = parse_qs(urlparse(raw_href).query)
            q_vals = qs.get("q") or qs.get("url") or []
            if not q_vals:
                continue
            target = q_vals[0]
        elif raw_href.startswith("/"):
            continue

        try:
            safe_url = validate_external_url(target, allow_test_origins=allow_test_origins)
        except UnsafeURLError:
            continue

        host = (urlparse(safe_url).hostname or "").lower()
        if host in _GOOGLE_INTERNAL_HOSTS or host.endswith(".google.com") or host.endswith(".gstatic.com"):
            continue

        if safe_url not in seen:
            seen.add(safe_url)
            candidates.append(safe_url)
        if len(candidates) >= limit:
            break

    return candidates


def extract_company_page_jobs(
    html: str,
    page_url: str,
    *,
    allow_test_origins: set[str] | None = None,
) -> list[NormalizedJob]:
    """Extract jobs from a company/ATS page via JSON-LD first, then fallback DOM."""
    jsonld_jobs = extract_job_postings_from_jsonld(
        html,
        page_url,
        source_name="google",
        allow_test_origins=allow_test_origins,
    )
    if jsonld_jobs:
        return jsonld_jobs

    blocker = detect_portal_blocker(html, page_url, portal="Página externa")
    if blocker:
        return []

    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else (soup.title.get_text(" ", strip=True) if soup.title else "")
    if not title or len(title) < 4:
        return []

    main_el = soup.find("main") or soup.find("article") or soup.body
    body_text = main_el.get_text(" ", strip=True) if main_el else ""
    if len(body_text) < 40:
        return []

    host = (urlparse(page_url).hostname or "empresa").removeprefix("www.")
    company = host.split(".")[0].capitalize()
    loc_el = soup.select_one('[class*="location"], [data-testid*="location"], address')
    location = loc_el.get_text(" ", strip=True) if loc_el else None

    try:
        return [
            make_browser_job(
                source="google",
                source_job_id=page_url,
                company=company,
                title=title,
                location=location,
                description=body_text[:4000],
                job_url=page_url,
                apply_url=page_url,
                attribution=f"Google Busca ({host})",
                workplace_hint=body_text[:400],
                allow_test_origins=allow_test_origins,
            )
        ]
    except Exception:
        return []


class GoogleCareersBrowserCollector(BrowserCollector):
    """Searches Google in CDP Chrome and extracts schema.org/JobPosting or career pages."""

    name = "google"

    async def collect(
        self,
        session: BrowserSession,
        plan: BrowserDiscoveryPlan,
    ) -> list[NormalizedJob]:
        queries = generate_search_queries(
            QueryGenerationConfig(
                roles=plan.roles,
                skills=[],
                locations=[plan.location] if plan.location else ["Recife PE", "Brasil"],
                remote_only=plan.remote_only,
                include_remote=plan.include_remote,
                max_queries=3,
            )
        )
        base = (plan.base_url_override or "https://www.google.com/search").rstrip("/")
        candidate_urls: list[str] = []
        seen_urls: set[str] = set()
        jobs: list[NormalizedJob] = []
        seen_job_urls: set[str] = set()

        async with session.discovery_page() as page:
            for idx, query_str in enumerate(queries[:2]):
                if idx > 0:
                    await asyncio.sleep(0.5)
                sep = "&" if "?" in base else "?"
                serp_url = f"{base}{sep}q={quote_plus(query_str)}&hl=pt-BR"
                final_serp_url = await session.safe_goto(page, serp_url)
                html = await page.content()
                for cand_url in parse_google_results_html(
                    html,
                    final_serp_url,
                    allow_test_origins=session.allow_test_origins or None,
                    limit=6,
                ):
                    if cand_url not in seen_urls:
                        seen_urls.add(cand_url)
                        candidate_urls.append(cand_url)

            for cand_url in candidate_urls[:6]:
                await asyncio.sleep(0.35)
                with suppress(Exception):
                    final_page_url = await session.safe_goto(page, cand_url, timeout_ms=15000)
                    page_html = await page.content()
                    for item in extract_company_page_jobs(
                        page_html,
                        final_page_url,
                        allow_test_origins=session.allow_test_origins or None,
                    ):
                        if item.job_url in seen_job_urls:
                            continue
                        seen_job_urls.add(item.job_url)
                        jobs.append(item)
                        if len(jobs) >= plan.limit_per_source:
                            return jobs[: plan.limit_per_source]

        return jobs[: plan.limit_per_source]
