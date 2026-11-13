"""LinkedIn logged-in browser collector (no API keys, strict rate limits, authwall-aware)."""

from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

from backend.app.browser_automation.session import BrowserSession
from backend.app.connectors.browser.base import (
    BrowserCollector,
    BrowserDiscoveryPlan,
    CollectorBlockedError,
    detect_portal_blocker,
    make_browser_job,
)
from backend.app.connectors.search.query_generator import generate_portal_queries
from backend.app.schemas.contracts import NormalizedJob


def _extract_linkedin_job_id(url: str) -> str:
    m = re.search(r"/jobs/view/(\d+)", url)
    if m:
        return m.group(1)
    m2 = re.search(r"currentJobId=(\d+)", url)
    return m2.group(1) if m2 else url


def parse_linkedin_search_html(
    html: str,
    page_url: str,
    *,
    allow_test_origins: set[str] | None = None,
    limit: int = 15,
) -> list[NormalizedJob]:
    """Parse LinkedIn job search cards from rendered HTML with login-wall detection."""
    blocker = detect_portal_blocker(html, page_url, portal="LinkedIn")
    if blocker:
        raise CollectorBlockedError(blocker)

    soup = BeautifulSoup(html, "html.parser")
    cards = list(
        soup.select(
            "li.jobs-search-results__list-item, "
            "ul.jobs-search__results-list > li, "
            "[data-occludable-job-id], "
            "div.base-card"
        )
    )
    if not cards:
        cards = [a.parent for a in soup.select('a[href*="/jobs/view/"]') if a.parent is not None]

    jobs: list[NormalizedJob] = []
    seen_urls: set[str] = set()

    for card in cards:
        link = card.select_one('a[href*="/jobs/view/"], a.base-card__full-link, a.job-card-list__title')
        if link is None:
            continue
        href = str(link.get("href") or "").strip()
        if not href:
            continue
        abs_url = urljoin(page_url, href).split("?")[0]
        if abs_url in seen_urls:
            continue

        title_el = card.select_one(
            ".base-search-card__title, .job-card-list__title, h3, strong"
        ) or link
        company_el = card.select_one(
            ".base-search-card__subtitle, .job-card-container__primary-description, .artdeco-entity-lockup__subtitle, h4"
        )
        loc_el = card.select_one(
            ".job-search-card__location, .job-card-container__metadata-wrapper li, .artdeco-entity-lockup__caption"
        )
        snippet_el = card.select_one(".job-search-card__snippet, .job-card-list__insight, p")

        title = title_el.get_text(" ", strip=True)
        if not title:
            continue
        company = company_el.get_text(" ", strip=True) if company_el else "Empresa no LinkedIn"
        location = loc_el.get_text(" ", strip=True) if loc_el else None
        raw_desc = snippet_el.get_text(" ", strip=True) if snippet_el else f"{title} — {company} ({location or 'Brasil'})"

        try:
            job = make_browser_job(
                source="linkedin",
                source_job_id=_extract_linkedin_job_id(abs_url),
                company=company,
                title=title,
                location=location,
                description=raw_desc,
                job_url=abs_url,
                apply_url=abs_url,
                attribution="LinkedIn Jobs (navegador)",
                workplace_hint=card.get_text(" ", strip=True)[:300],
                allow_test_origins=allow_test_origins,
            )
            seen_urls.add(abs_url)
            jobs.append(job)
        except Exception:
            continue

        if len(jobs) >= limit:
            break

    return jobs


class LinkedInBrowserCollector(BrowserCollector):
    """Collects jobs from signed-in LinkedIn search results in the CDP Chrome context."""

    name = "linkedin"

    async def collect(
        self,
        session: BrowserSession,
        plan: BrowserDiscoveryPlan,
    ) -> list[NormalizedJob]:
        queries = generate_portal_queries(
            plan.roles,
            location=plan.location,
            remote_only=plan.remote_only,
            include_remote=plan.include_remote,
            max_queries=3,
        )
        base = (plan.base_url_override or "https://www.linkedin.com/jobs/search/").rstrip("/")
        collected: list[NormalizedJob] = []
        seen_urls: set[str] = set()

        async with session.discovery_page() as page:
            for idx, q in enumerate(queries):
                if idx > 0:
                    await asyncio.sleep(0.4)
                remote_param = "&f_WT=2" if q["mode"] == "remote" else ""
                sep = "&" if "?" in base else "?"
                search_url = (
                    f"{base}{sep}keywords={quote_plus(q['keywords'])}"
                    f"&location={quote_plus(q['location'])}{remote_param}"
                )
                final_url = await session.safe_goto(page, search_url)
                with suppress(Exception):
                    await page.wait_for_selector(
                        "li.jobs-search-results__list-item, div.base-card, a[href*='/jobs/view/']",
                        timeout=3500,
                    )
                html = await page.content()
                parsed = parse_linkedin_search_html(
                    html,
                    final_url,
                    allow_test_origins=session.allow_test_origins or None,
                    limit=max(1, plan.limit_per_source - len(collected)),
                )
                for item in parsed:
                    if item.job_url in seen_urls:
                        continue
                    seen_urls.add(item.job_url)
                    collected.append(item)
                if len(collected) >= plan.limit_per_source:
                    break

        return collected[: plan.limit_per_source]
