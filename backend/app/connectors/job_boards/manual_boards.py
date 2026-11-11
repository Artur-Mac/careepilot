"""Official alerts & manual-only URL normalization for Gupy, LinkedIn, Indeed, Programathor, Remotar."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from backend.app.connectors.base import JobSource, parse_timestamp
from backend.app.schemas.contracts import NormalizedJob, SearchQuery
from backend.tests.fixtures.mock_feeds import build_mock_manual_board_alerts


class ManualBoardAlertsConnector(JobSource):
    """Ingests official job alerts / user-provided URLs — strictly manual_only, zero prohibited scraping."""

    name = "manual_alerts"
    kind = "manual"
    default_mode = "manual_only"

    def normalize_manual_record(self, raw: dict[str, Any]) -> NormalizedJob:
        now = datetime.now(UTC)
        return NormalizedJob(
            source=str(raw.get("source") or self.name),
            source_job_id=str(raw.get("source_job_id") or "") or None,
            company=str(raw.get("company") or "Empresa não informada"),
            title=str(raw.get("title") or "Vaga importada"),
            location=str(raw.get("location") or "") or None,
            workplace_type=raw.get("workplace_type", "unknown"),
            description=self._clean_description(str(raw.get("description") or "")),
            job_url=str(raw["job_url"]),
            apply_url=str(raw.get("apply_url") or raw["job_url"]),
            posted_at=parse_timestamp(raw.get("posted_at")),
            discovered_at=now,
            employment_type=str(raw.get("employment_type") or "") or None,
            seniority=str(raw.get("seniority") or "") or None,
            salary_min=float(raw["salary_min"]) if raw.get("salary_min") is not None else None,
            salary_max=float(raw["salary_max"]) if raw.get("salary_max") is not None else None,
            salary_currency=str(raw.get("salary_currency") or "") or None,
            # Platform policies forbid automated bots/scraping on LinkedIn/Indeed/Gupy.
            apply_mode="manual_only",
            source_attribution=str(raw.get("source_attribution") or "Alerta oficial / link manual"),
        )

    async def discover(self, query: SearchQuery) -> list[NormalizedJob]:
        items = build_mock_manual_board_alerts()
        return [self.normalize_manual_record(item) for item in items][: query.limit]
