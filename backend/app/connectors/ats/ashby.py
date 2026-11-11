"""Ashby public Job Postings API connector."""

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
from backend.tests.fixtures.mock_feeds import build_mock_ashby_payload


class AshbyConnector(JobSource):
    """Connector for https://api.ashbyhq.com/posting-api/job-board/{board_name}?includeCompensation=true."""

    name = "ashby"
    kind = "ats"

    def __init__(
        self,
        settings: Any,
        *,
        board_names: list[str] | None = None,
        use_live_network: bool = False,
        http_client: Any = None,
    ) -> None:
        super().__init__(settings, use_live_network=use_live_network, http_client=http_client)
        self.board_names = board_names or ["example-ashby"]

    def parse_payload(self, payload: dict[str, Any], board_name: str) -> list[NormalizedJob]:
        jobs: list[NormalizedJob] = []
        now = datetime.now(UTC)
        for item in payload.get("jobs") or []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            job_url = str(item.get("jobUrl") or "").strip()
            if not title or not job_url:
                continue
            loc = item.get("location")
            desc_raw = item.get("descriptionPlain") or item.get("descriptionHtml") or title
            desc = self._clean_description(str(desc_raw))
            comp = item.get("compensation") or {}
            smin: float | None = None
            smax: float | None = None
            scur: str | None = None
            if isinstance(comp, dict) and comp.get("summaryComponents"):
                first = comp["summaryComponents"][0]
                if isinstance(first, dict):
                    smin = float(first["minValue"]) if first.get("minValue") is not None else None
                    smax = float(first["maxValue"]) if first.get("maxValue") is not None else None
                    scur = str(first.get("currencyCode") or "") or None
            company = str(item.get("companyName") or board_name).strip()
            try:
                jobs.append(
                    NormalizedJob(
                        source=self.name,
                        source_job_id=str(item.get("id") or job_url),
                        company=company,
                        title=title,
                        location=str(loc) if loc else None,
                        workplace_type=infer_workplace_type(
                            f"{item.get('workplaceType') or ''} {loc or ''}",
                            is_remote=item.get("isRemote"),
                        ),
                        description=desc,
                        job_url=job_url,
                        apply_url=str(item.get("applyUrl") or job_url),
                        posted_at=parse_timestamp(item.get("publishedAt")),
                        discovered_at=now,
                        employment_type=str(item.get("employmentType") or "") or None,
                        seniority=infer_seniority(title),
                        salary_min=smin,
                        salary_max=smax,
                        salary_currency=scur,
                        apply_mode="assisted_manual_submit",
                        source_attribution=f"Ashby Job Postings ({company})",
                    )
                )
            except Exception:
                continue
        return jobs

    async def discover(self, query: SearchQuery) -> list[NormalizedJob]:
        results: list[NormalizedJob] = []
        if not self.use_live_network and self._http_client is None:
            results.extend(self.parse_payload(build_mock_ashby_payload(), "fictional"))
        else:
            for board in self.board_names[:5]:
                url = f"https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true"
                payload = await self._get_json(url)
                if isinstance(payload, dict):
                    results.extend(self.parse_payload(payload, board))
        return results[: query.limit]
