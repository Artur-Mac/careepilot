"""Lever Postings API connector supporting global and EU regional endpoints."""

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
from backend.tests.fixtures.mock_feeds import build_mock_lever_payload


class LeverConnector(JobSource):
    """Connector for https://api.lever.co/v0/postings/{site}?mode=json and EU variant."""

    name = "lever"
    kind = "ats"

    def __init__(
        self,
        settings: Any,
        *,
        sites: list[tuple[str, str]] | None = None,
        use_live_network: bool = False,
        http_client: Any = None,
    ) -> None:
        super().__init__(settings, use_live_network=use_live_network, http_client=http_client)
        # Tuple of (site_slug, region: "global" | "eu")
        self.sites = sites or [("leverdemo", "global")]

    def parse_payload(self, payload: list[Any], site: str) -> list[NormalizedJob]:
        jobs: list[NormalizedJob] = []
        now = datetime.now(UTC)
        for item in payload:
            if not isinstance(item, dict):
                continue
            title = str(item.get("text") or "").strip()
            job_url = str(item.get("hostedUrl") or "").strip()
            if not title or not job_url:
                continue
            raw_cats = item.get("categories")
            cats: dict[str, Any] = raw_cats if isinstance(raw_cats, dict) else {}
            loc = cats.get("location")
            emp = cats.get("commitment")
            desc = self._clean_description(
                str(item.get("descriptionPlain") or item.get("description") or title)
            )
            company = str(item.get("company") or site).strip()
            try:
                jobs.append(
                    NormalizedJob(
                        source=self.name,
                        source_job_id=str(item.get("id") or ""),
                        company=company,
                        title=title,
                        location=str(loc) if loc else None,
                        workplace_type=infer_workplace_type(
                            f"{item.get('workplaceType') or ''} {loc or ''} {desc}"
                        ),
                        description=desc,
                        job_url=job_url,
                        apply_url=str(item.get("applyUrl") or job_url),
                        posted_at=parse_timestamp(item.get("createdAt")),
                        discovered_at=now,
                        employment_type=str(emp) if emp else None,
                        seniority=infer_seniority(title),
                        apply_mode="assisted_manual_submit",
                        source_attribution=f"Lever Postings ({company})",
                    )
                )
            except Exception:
                continue
        return jobs

    async def discover(self, query: SearchQuery) -> list[NormalizedJob]:
        results: list[NormalizedJob] = []
        if not self.use_live_network and self._http_client is None:
            results.extend(self.parse_payload(build_mock_lever_payload(), "fictional"))
        else:
            for site, region in self.sites[:5]:
                base = "https://api.eu.lever.co" if region == "eu" else "https://api.lever.co"
                url = f"{base}/v0/postings/{site}?mode=json"
                payload = await self._get_json(url)
                if isinstance(payload, list):
                    results.extend(self.parse_payload(payload, site))
        return results[: query.limit]
