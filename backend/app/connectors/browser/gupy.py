"""Gupy portal browser collector extracting job cards and JSON-LD details."""

from __future__ import annotations

import asyncio
import json
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
    enrich_job_details,
    goto_with_transient_retry,
    make_browser_job,
)
from backend.app.connectors.search.company_jsonld import extract_job_postings_from_jsonld
from backend.app.connectors.search.query_generator import generate_portal_queries
from backend.app.schemas.contracts import NormalizedJob


def _extract_gupy_id(url: str) -> str:
    m = re.search(r"/jobs/(\d+)", url)
    return f"gupy-{m.group(1)}" if m else url


def parse_gupy_search_html(
    html: str,
    page_url: str,
    *,
    allow_test_origins: set[str] | None = None,
    limit: int = 15,
) -> list[NormalizedJob]:
    """Parse Gupy job search results from rendered HTML, Next.js state, or JSON-LD."""
    blocker = detect_portal_blocker(html, page_url, portal="Gupy")
    if blocker:
        raise CollectorBlockedError(blocker)

    jobs: list[NormalizedJob] = []
    seen_urls: set[str] = set()

    # 1. Try schema.org JSON-LD first if rendered on page
    for jld in extract_job_postings_from_jsonld(
        html,
        page_url,
        source_name="gupy",
        allow_test_origins=allow_test_origins,
    ):
        if jld.job_url not in seen_urls:
            seen_urls.add(jld.job_url)
            jobs.append(jld)
            if len(jobs) >= limit:
                return jobs

    soup = BeautifulSoup(html, "html.parser")

    # 2. Try __NEXT_DATA__ embedded JSON if present
    next_script = soup.find("script", attrs={"id": "__NEXT_DATA__"})
    if next_script and next_script.string:
        with suppress(json.JSONDecodeError, TypeError, ValueError):
            payload = json.loads(next_script.string)
            candidates: list[dict[str, object]] = []
            stack: list[object] = [payload]
            while stack:
                node = stack.pop()
                if isinstance(node, dict):
                    if "name" in node and "jobUrl" in node and "careerPageName" in node:
                        candidates.append(node)
                    else:
                        stack.extend(node.values())
                elif isinstance(node, list):
                    stack.extend(node)
            for item in candidates:
                raw_url = str(item.get("jobUrl") or "").strip()
                title = str(item.get("name") or "").strip()
                if not raw_url or not title:
                    continue
                abs_url = urljoin(page_url, raw_url).split("?")[0]
                if abs_url in seen_urls:
                    continue
                company = str(item.get("careerPageName") or "Empresa na Gupy")
                city = str(item.get("city") or "").strip()
                state = str(item.get("state") or "").strip()
                country = str(item.get("country") or "").strip()
                wp_raw = str(item.get("workplaceType") or "")
                loc = ", ".join(p for p in (city, state, country) if p) or None
                desc = str(item.get("description") or f"{title} em {company}. Modalidade: {wp_raw or 'N/I'}.")
                try:
                    job = make_browser_job(
                        source="gupy",
                        source_job_id=str(item.get("id") or _extract_gupy_id(abs_url)),
                        company=company,
                        title=title,
                        location=loc,
                        description=desc,
                        job_url=abs_url,
                        apply_url=abs_url,
                        attribution=f"Gupy ({company})",
                        workplace_hint=wp_raw,
                        allow_test_origins=allow_test_origins,
                    )
                    seen_urls.add(abs_url)
                    jobs.append(job)
                except Exception:
                    continue
                if len(jobs) >= limit:
                    return jobs

    # 3. Parse rendered HTML cards linking to Gupy job pages
    for link in soup.select('a[href*="/jobs/"], a[href*="gupy.io"]'):
        href = str(link.get("href") or "").strip()
        if not href or ("/jobs/" not in href and "job" not in href.lower()):
            continue
        abs_url = urljoin(page_url, href).split("?")[0]
        if abs_url in seen_urls:
            continue
        card = link.find_parent(["li", "article", "div"]) or link
        title_el = card.select_one("h2, h3, [data-testid*='job-title']") or link
        company_el = card.select_one("[data-testid*='company'], .company-name, span.company")
        loc_el = card.select_one("[data-testid*='location'], .job-location, address")
        title = title_el.get_text(" ", strip=True)
        if not title or len(title) < 3:
            continue
        company = company_el.get_text(" ", strip=True) if company_el else "Empresa na Gupy"
        location = loc_el.get_text(" ", strip=True) if loc_el else None
        card_text = card.get_text(" ", strip=True)
        try:
            job = make_browser_job(
                source="gupy",
                source_job_id=_extract_gupy_id(abs_url),
                company=company,
                title=title,
                location=location,
                description=card_text[:1500] or title,
                job_url=abs_url,
                apply_url=abs_url,
                attribution=f"Gupy ({company})",
                workplace_hint=card_text[:300],
                allow_test_origins=allow_test_origins,
            )
            seen_urls.add(abs_url)
            jobs.append(job)
        except Exception:
            continue
        if len(jobs) >= limit:
            break

    return jobs


class GupyBrowserCollector(BrowserCollector):
    """Collects jobs from Gupy portal search using persistent CDP Chrome."""

    name = "gupy"

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
            max_queries=2,
        )
        base = (plan.base_url_override or "https://portal.gupy.io/vagas").rstrip("/")
        collected: list[NormalizedJob] = []
        seen_urls: set[str] = set()

        async with session.discovery_page() as page:
            for idx, q in enumerate(queries[:2]):
                if idx > 0:
                    await asyncio.sleep(0.4 if session.allow_test_origins else 2)
                sep = "&" if "?" in base else "?"
                remote_q = "&workplaceTypes[]=remote" if q["mode"] == "remote" else ""
                search_url = f"{base}{sep}searchTerm={quote_plus(q['keywords'])}{remote_q}"
                final_url = await goto_with_transient_retry(session, page, search_url)
                with suppress(Exception):
                    await page.wait_for_selector("a[href*='/jobs/'], main", timeout=3500)
                html = await page.content()
                parsed = parse_gupy_search_html(
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

            await enrich_job_details(page, session, collected)

        return collected[: plan.limit_per_source]
