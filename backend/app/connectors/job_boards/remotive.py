"""Remotive Public API connector honoring backlink and attribution terms."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from backend.app.connectors.base import (
    JobSource,
    infer_seniority,
    parse_salary_string,
    parse_timestamp,
)
from backend.app.schemas.contracts import NormalizedJob, SearchQuery
from backend.tests.fixtures.mock_feeds import build_mock_remotive_payload


class RemotiveConnector(JobSource):
    """Connector for https://remotive.com/api/remote-jobs."""

    name = "remotive"
    kind = "job_board"

    def parse_payload(self, payload: dict[str, Any]) -> list[NormalizedJob]:
        jobs: list[NormalizedJob] = []
        now = datetime.now(UTC)
        for item in payload.get("jobs") or []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            job_url = str(item.get("url") or "").strip()
            if not title or not job_url:
                continue
            smin, smax, scur = parse_salary_string(item.get("salary"))
            desc = self._clean_description(str(item.get("description") or title))
            company = str(item.get("company_name") or "Unknown").strip()
            try:
                jobs.append(
                    NormalizedJob(
                        source=self.name,
                        source_job_id=str(item.get("id") or ""),
                        company=company,
                        title=title,
                        location=str(item.get("candidate_required_location") or "Remote"),
                        workplace_type="remote",
                        description=desc,
                        job_url=job_url,
                        apply_url=job_url,
                        posted_at=parse_timestamp(item.get("publication_date")),
                        discovered_at=now,
                        employment_type=str(item.get("job_type") or "") or None,
                        seniority=infer_seniority(title),
                        salary_min=smin,
                        salary_max=smax,
                        salary_currency=scur,
                        apply_mode="assisted_manual_submit",
                        source_attribution="Remotive Remote Jobs API (remotive.com)",
                    )
                )
            except Exception:
                continue
        return jobs

    async def discover(self, query: SearchQuery) -> list[NormalizedJob]:
        if not self.use_live_network and self._http_client is None:
            return self.parse_payload(build_mock_remotive_payload())[: query.limit]

        payload = await self._get_json(
            "https://remotive.com/api/remote-jobs",
            params={"search": query.text, "limit": min(query.limit, 50)},
        )
        if not isinstance(payload, dict):
            return []
        return self.parse_payload(payload)[: query.limit]
