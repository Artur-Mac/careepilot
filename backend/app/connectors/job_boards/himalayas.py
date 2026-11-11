"""Himalayas Remote Jobs API connector honoring cursor pagination and applicationLink attribution."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from backend.app.connectors.base import JobSource, infer_seniority, parse_timestamp
from backend.app.schemas.contracts import NormalizedJob, SearchQuery
from backend.tests.fixtures.mock_feeds import build_mock_himalayas_payload


class HimalayasConnector(JobSource):
    """Connector for https://himalayas.app/jobs/api and /jobs/api/search."""

    name = "himalayas"
    kind = "job_board"

    def parse_payload(self, payload: dict[str, Any]) -> list[NormalizedJob]:
        jobs: list[NormalizedJob] = []
        now = datetime.now(UTC)
        for item in payload.get("jobs") or []:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            app_link = str(item.get("applicationLink") or "").strip()
            guid = str(item.get("guid") or app_link).strip()
            if not title or not (app_link or guid):
                continue

            locs = item.get("locationRestrictions") or []
            loc_names: list[str] = []
            for entry in locs:
                if isinstance(entry, dict) and entry.get("name"):
                    loc_names.append(str(entry["name"]))
                elif isinstance(entry, str):
                    loc_names.append(entry)
            location_str = ", ".join(loc_names) if loc_names else "Remote (Worldwide)"

            sen_raw = item.get("seniority")
            sen_str = ", ".join(sen_raw) if isinstance(sen_raw, list) else (str(sen_raw) if sen_raw else None)

            smin = float(item["minSalary"]) if item.get("minSalary") is not None else None
            smax = float(item["maxSalary"]) if item.get("maxSalary") is not None else None
            period = str(item.get("salaryPeriod") or "annual").lower()
            if smin is not None and period == "hourly":
                # Keep raw values or let UI display; don't fabricate conversions silently.
                pass

            company = str(item.get("companyName") or "Unknown").strip()
            desc = self._clean_description(str(item.get("description") or item.get("excerpt") or title))

            try:
                jobs.append(
                    NormalizedJob(
                        source=self.name,
                        source_job_id=guid,
                        company=company,
                        title=title,
                        location=location_str,
                        workplace_type="remote",
                        description=desc,
                        job_url=guid if guid.startswith("http") else app_link,
                        apply_url=app_link or guid,
                        posted_at=parse_timestamp(item.get("pubDate")),
                        discovered_at=now,
                        employment_type=str(item.get("employmentType") or "") or None,
                        seniority=infer_seniority(title, sen_str),
                        salary_min=smin,
                        salary_max=smax,
                        salary_currency=str(item.get("currency") or "") or None,
                        apply_mode="assisted_manual_submit",
                        source_attribution="Himalayas Remote Jobs API (himalayas.app)",
                    )
                )
            except Exception:
                continue
        return jobs

    async def discover(self, query: SearchQuery) -> list[NormalizedJob]:
        if not self.use_live_network and self._http_client is None:
            return self.parse_payload(build_mock_himalayas_payload())[: query.limit]

        headers: dict[str, str] = {}
        if self.settings.himalayas_api_key:
            headers["Authorization"] = f"Bearer {self.settings.himalayas_api_key}"

        collected: list[NormalizedJob] = []
        cursor: str | None = None
        for _ in range(3):  # Bounded pagination
            params: dict[str, Any] = {"limit": min(20, query.limit)}
            if cursor:
                params["cursor"] = cursor
            payload = await self._get_json(
                "https://himalayas.app/jobs/api", params=params, headers=headers or None
            )
            if not isinstance(payload, dict):
                break
            collected.extend(self.parse_payload(payload))
            cursor = payload.get("nextCursor")
            if not cursor or len(collected) >= query.limit:
                break
        return collected[: query.limit]
