"""Optional Tavily and Adzuna search connectors (strictly disabled without explicit API keys)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from backend.app.connectors.base import (
    JobSource,
    infer_seniority,
    infer_workplace_type,
    parse_timestamp,
)
from backend.app.schemas.contracts import NormalizedJob, SearchQuery


class TavilySearchConnector(JobSource):
    """Optional Tavily web search connector; enabled only when TAVILY_API_KEY is supplied."""

    name = "tavily_search"
    kind = "search"
    default_mode = "credential_required"

    async def discover(self, query: SearchQuery) -> list[NormalizedJob]:
        api_key = self.settings.tavily_api_key.strip()
        if not api_key or (not self.use_live_network and self._http_client is None):
            return []
        # Placeholder interface ready for POST https://api.tavily.com/search when live key is supplied.
        return []


class AdzunaConnector(JobSource):
    """Optional Adzuna Jobs Search API connector; requires ADZUNA_APP_ID and ADZUNA_APP_KEY."""

    name = "adzuna"
    kind = "search"
    default_mode = "credential_required"

    def parse_payload(self, payload: dict[str, Any]) -> list[NormalizedJob]:
        jobs: list[NormalizedJob] = []
        now = datetime.now(UTC)
        for item in payload.get("results") or []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            url = str(item.get("redirect_url") or "").strip()
            if not title or not url:
                continue
            company = str((item.get("company") or {}).get("display_name") or "Empresa Adzuna")
            loc = str((item.get("location") or {}).get("display_name") or "") or None
            desc = self._clean_description(str(item.get("description") or title))
            try:
                jobs.append(
                    NormalizedJob(
                        source=self.name,
                        source_job_id=str(item.get("id") or ""),
                        company=company,
                        title=title,
                        location=loc,
                        workplace_type=infer_workplace_type(f"{title} {loc or ''} {desc}"),
                        description=desc,
                        job_url=url,
                        apply_url=url,
                        posted_at=parse_timestamp(item.get("created")),
                        discovered_at=now,
                        seniority=infer_seniority(title),
                        salary_min=float(item["salary_min"]) if item.get("salary_min") is not None else None,
                        salary_max=float(item["salary_max"]) if item.get("salary_max") is not None else None,
                        apply_mode="assisted_manual_submit",
                        source_attribution="Adzuna Job Search API",
                    )
                )
            except Exception:
                continue
        return jobs

    async def discover(self, query: SearchQuery) -> list[NormalizedJob]:
        app_id = self.settings.adzuna_app_id.strip()
        app_key = self.settings.adzuna_app_key.strip()
        if (not app_id or not app_key) and self._http_client is None:
            return []
        if not self.use_live_network and self._http_client is None:
            return []
        payload = await self._get_json(
            "https://api.adzuna.com/v1/api/jobs/br/search/1",
            params={
                "app_id": app_id or "test-id",
                "app_key": app_key or "test-key",
                "what": query.text,
                "results_per_page": min(20, query.limit),
            },
        )
        if not isinstance(payload, dict):
            return []
        return self.parse_payload(payload)[: query.limit]
