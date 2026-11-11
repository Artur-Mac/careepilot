"""Fictional local employer ATS connector for offline E2E Playwright demonstration."""

from __future__ import annotations

from datetime import UTC, datetime

from backend.app.connectors.base import JobSource, parse_timestamp
from backend.app.schemas.contracts import NormalizedJob, SearchQuery
from backend.tests.fixtures.mock_feeds import build_mock_fictional_test_ats_jobs


class FictionalTestAtsConnector(JobSource):
    """Provides strictly fictional local test ATS jobs bound to loopback TEST_ATS_URL."""

    name = "fictional_ats"
    kind = "ats"
    default_mode = "test_fixture"

    async def discover(self, query: SearchQuery) -> list[NormalizedJob]:
        if not self.settings.test_ats_enabled or not self.settings.test_ats_origin:
            return []
        now = datetime.now(UTC)
        items = build_mock_fictional_test_ats_jobs(self.settings.test_ats_origin)
        allow_origins = {self.settings.test_ats_origin}
        jobs: list[NormalizedJob] = []
        for item in items:
            jobs.append(
                NormalizedJob.model_validate(
                    {
                        "source": str(item["source"]),
                        "source_job_id": str(item["source_job_id"]),
                        "company": str(item["company"]),
                        "title": str(item["title"]),
                        "location": str(item["location"]),
                        "workplace_type": item["workplace_type"],
                        "description": str(item["description"]),
                        "job_url": str(item["job_url"]),
                        "apply_url": str(item["apply_url"]),
                        "posted_at": parse_timestamp(item["posted_at"]),
                        "discovered_at": now,
                        "employment_type": str(item["employment_type"]),
                        "seniority": str(item["seniority"]),
                        "salary_min": float(item["salary_min"]),  # type: ignore[arg-type]
                        "salary_max": float(item["salary_max"]),  # type: ignore[arg-type]
                        "salary_currency": str(item["salary_currency"]),
                        "apply_mode": item["apply_mode"],
                        "source_attribution": str(item["source_attribution"]),
                    },
                    context={"allow_test_origins": allow_origins},
                )
            )
        return jobs[: query.limit]
