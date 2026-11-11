"""Greenhouse public Job Board API connector (GET read-only; never uses employer POST as candidate)."""

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
from backend.tests.fixtures.mock_feeds import build_mock_greenhouse_payload


class GreenhouseConnector(JobSource):
    """Connector for https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true."""

    name = "greenhouse"
    kind = "ats"

    def __init__(
        self,
        settings: Any,
        *,
        board_tokens: list[str] | None = None,
        use_live_network: bool = False,
        http_client: Any = None,
    ) -> None:
        super().__init__(settings, use_live_network=use_live_network, http_client=http_client)
        self.board_tokens = board_tokens or ["example-board"]

    def parse_payload(self, payload: dict[str, Any], board_token: str) -> list[NormalizedJob]:
        jobs: list[NormalizedJob] = []
        now = datetime.now(UTC)
        for item in payload.get("jobs") or []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            job_url = str(item.get("absolute_url") or "").strip()
            if not title or not job_url:
                continue
            loc = ((item.get("location") or {}).get("name") if isinstance(item.get("location"), dict) else None)
            desc = self._clean_description(str(item.get("content") or title))
            company = str(item.get("company_name") or board_token).strip()
            try:
                jobs.append(
                    NormalizedJob(
                        source=self.name,
                        source_job_id=str(item.get("id") or ""),
                        company=company,
                        title=title,
                        location=str(loc) if loc else None,
                        workplace_type=infer_workplace_type(f"{title} {loc or ''} {desc}"),
                        description=desc,
                        job_url=job_url,
                        apply_url=job_url,
                        posted_at=parse_timestamp(item.get("updated_at")),
                        discovered_at=now,
                        seniority=infer_seniority(title),
                        # Public read API does not grant automated candidate submission permission.
                        apply_mode="assisted_manual_submit",
                        source_attribution=f"Greenhouse Job Board ({company})",
                    )
                )
            except Exception:
                # Skip malformed individual records without crashing the whole feed.
                continue
        return jobs

    async def discover(self, query: SearchQuery) -> list[NormalizedJob]:
        results: list[NormalizedJob] = []
        if not self.use_live_network and self._http_client is None:
            results.extend(self.parse_payload(build_mock_greenhouse_payload(), "fictional"))
        else:
            for token in self.board_tokens[:5]:
                url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
                payload = await self._get_json(url)
                if isinstance(payload, dict):
                    results.extend(self.parse_payload(payload, token))
        return results[: query.limit]
