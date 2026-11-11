"""Brave Search API connector (enabled only when BRAVE_SEARCH_API_KEY is explicitly configured)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from backend.app.connectors.base import JobSource, infer_seniority, infer_workplace_type
from backend.app.schemas.contracts import NormalizedJob, SearchQuery


class BraveSearchConnector(JobSource):
    """Discovers candidate job posting links via Brave Search API without masquerading traffic."""

    name = "brave_search"
    kind = "search"
    default_mode = "credential_required"

    def parse_results(self, payload: dict[str, Any]) -> list[NormalizedJob]:
        jobs: list[NormalizedJob] = []
        now = datetime.now(UTC)
        raw_web = payload.get("web")
        web: dict[str, Any] = raw_web if isinstance(raw_web, dict) else {}
        for item in web.get("results") or []:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            title = str(item.get("title") or "").strip()
            if not url or not title:
                continue
            desc = self._clean_description(str(item.get("description") or title))
            # Classify apply mode conservatively by host.
            try:
                jobs.append(
                    NormalizedJob(
                        source=self.name,
                        source_job_id=url,
                        company=str(item.get("profile", {}).get("name") or "Empresa via Busca"),
                        title=title,
                        location=None,
                        workplace_type=infer_workplace_type(f"{title} {desc}"),
                        description=desc,
                        job_url=url,
                        apply_url=url,
                        posted_at=None,
                        discovered_at=now,
                        seniority=infer_seniority(title),
                        apply_mode="manual_only",
                        source_attribution="Brave Web Search API",
                    )
                )
            except Exception:
                continue
        return jobs

    async def discover(self, query: SearchQuery) -> list[NormalizedJob]:
        api_key = self.settings.brave_search_api_key.strip()
        if not api_key and self._http_client is None:
            return []
        if not self.use_live_network and self._http_client is None:
            return []

        keywords = " ".join(filter(None, [query.text, query.location, "remoto OR remote" if query.remote_only else ""]))
        queries = [
            f"{keywords} (vaga OR jobs OR careers)",
            f"{keywords} (site:jobs.ashbyhq.com OR site:jobs.lever.co OR site:job-boards.greenhouse.io)",
        ]
        collected: list[NormalizedJob] = []
        for q in queries:
            payload = await self._get_json(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": q, "count": min(10, query.limit)},
                headers={"X-Subscription-Token": api_key or "test-key", "Accept": "application/json"},
            )
            if isinstance(payload, dict):
                collected.extend(self.parse_results(payload))
        return collected[: query.limit]
