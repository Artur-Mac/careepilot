"""Arbeitnow Job Board API connector."""

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
from backend.tests.fixtures.mock_feeds import build_mock_arbeitnow_payload


class ArbeitnowConnector(JobSource):
    """Connector for https://www.arbeitnow.com/api/job-board-api."""

    name = "arbeitnow"
    kind = "job_board"

    def parse_payload(self, payload: dict[str, Any]) -> list[NormalizedJob]:
        jobs: list[NormalizedJob] = []
        now = datetime.now(UTC)
        for item in payload.get("data") or []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            job_url = str(item.get("url") or "").strip()
            if not title or not job_url:
                continue
            loc = str(item.get("location") or "") or None
            jtypes = item.get("job_types") or []
            emp = ", ".join(str(t) for t in jtypes) if isinstance(jtypes, list) and jtypes else None
            desc = self._clean_description(str(item.get("description") or title))
            company = str(item.get("company_name") or "Unknown").strip()
            try:
                jobs.append(
                    NormalizedJob(
                        source=self.name,
                        source_job_id=str(item.get("slug") or ""),
                        company=company,
                        title=title,
                        location=loc,
                        workplace_type=infer_workplace_type(
                            f"{title} {loc or ''}",
                            is_remote=bool(item.get("remote")),
                        ),
                        description=desc,
                        job_url=job_url,
                        apply_url=job_url,
                        posted_at=parse_timestamp(item.get("created_at")),
                        discovered_at=now,
                        employment_type=emp,
                        seniority=infer_seniority(title),
                        apply_mode="assisted_manual_submit",
                        source_attribution="Arbeitnow Job Board API (arbeitnow.com)",
                    )
                )
            except Exception:
                continue
        return jobs

    async def discover(self, query: SearchQuery) -> list[NormalizedJob]:
        if not self.use_live_network and self._http_client is None:
            return self.parse_payload(build_mock_arbeitnow_payload())[: query.limit]

        payload = await self._get_json("https://www.arbeitnow.com/api/job-board-api")
        if not isinstance(payload, dict):
            return []
        return self.parse_payload(payload)[: query.limit]
