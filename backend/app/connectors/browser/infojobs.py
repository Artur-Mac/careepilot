"""InfoJobs Brasil browser collector extracting PT-BR job cards safely."""

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


def _extract_infojobs_id(url: str) -> str:
    m = re.search(r"__(\d+)(?:\.aspx)?", url)
    if m:
        return f"infojobs-{m.group(1)}"
    m2 = re.search(r"(\d{5,})", url)
    return f"infojobs-{m2.group(1)}" if m2 else url


def parse_infojobs_search_html(
    html: str,
    page_url: str,
    *,
    allow_test_origins: set[str] | None = None,
    limit: int = 15,
) -> list[NormalizedJob]:
    """Parse InfoJobs Brasil job search cards from rendered HTML."""
    blocker = detect_portal_blocker(html, page_url, portal="InfoJobs")
    if blocker:
        raise CollectorBlockedError(blocker)

    soup = BeautifulSoup(html, "html.parser")
    cards = list(
        soup.select(".js_rowCard, article[data-id], div.card-shadow, [data-href*='/vaga-de-']")
    )
    if not cards:
        cards = [
            a.find_parent(["div", "article", "li"]) or a
            for a in soup.select('a[href*="/vaga-de-"]')
        ]

    jobs: list[NormalizedJob] = []
    seen_urls: set[str] = set()

    for card in cards:
        link = card.select_one('a[href*="/vaga-de-"], h2 a, a.js_ofrLink')
        href = ""
        if link is not None:
            href = str(link.get("href") or "").strip()
        elif card.get("data-href"):
            href = str(card.get("data-href") or "").strip()
        if not href:
            continue

        abs_url = urljoin(page_url, href).split("?")[0]
        if abs_url in seen_urls:
            continue

        title_el = card.select_one("h2, .h3, .js_ofrTitle, a[href*='/vaga-de-']") or link
        company_el = card.select_one(
            ".text-body a, .js_companyLink, .company-name, [data-company], span.text-body"
        )
        loc_el = card.select_one(".small.text-medium, .location, [data-city], .city")
        desc_el = card.select_one(".small.text-dark, .description, p")

        title = title_el.get_text(" ", strip=True) if title_el else ""
        if not title:
            continue
        company = company_el.get_text(" ", strip=True) if company_el else "Empresa no InfoJobs"
        location = loc_el.get_text(" ", strip=True) if loc_el else None
        card_text = card.get_text(" ", strip=True)
        description = desc_el.get_text(" ", strip=True) if desc_el else card_text[:1500]

        try:
            job = make_browser_job(
                source="infojobs",
                source_job_id=str(card.get("data-id") or _extract_infojobs_id(abs_url)),
                company=company,
                title=title,
                location=location,
                description=description or title,
                job_url=abs_url,
                apply_url=abs_url,
                attribution="InfoJobs Brasil (navegador)",
                workplace_hint=card_text[:400],
                allow_test_origins=allow_test_origins,
            )
            seen_urls.add(abs_url)
            jobs.append(job)
        except Exception:
            continue

        if len(jobs) >= limit:
            break

    return jobs


class InfoJobsBrowserCollector(BrowserCollector):
    """Collects jobs from InfoJobs Brasil using persistent CDP Chrome."""

    name = "infojobs"

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
        base = (plan.base_url_override or "https://www.infojobs.com.br/empregos.aspx").rstrip("/")
        collected: list[NormalizedJob] = []
        seen_urls: set[str] = set()

        async with session.discovery_page() as page:
            for idx, q in enumerate(queries):
                if idx > 0:
                    await asyncio.sleep(0.4)
                sep = "&" if "?" in base else "?"
                mode_kw = " remoto" if q["mode"] == "remote" else ""
                search_url = f"{base}{sep}palabra={quote_plus(q['keywords'] + mode_kw)}"
                final_url = await session.safe_goto(page, search_url)
                with suppress(Exception):
                    await page.wait_for_selector(
                        ".js_rowCard, a[href*='/vaga-de-'], main",
                        timeout=3500,
                    )
                html = await page.content()
                parsed = parse_infojobs_search_html(
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
