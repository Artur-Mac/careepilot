"""SmartRecruiters connector (public postings GET only; credentialed endpoints disabled without lawful access)."""

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


class SmartRecruitersConnector(JobSource):
    """Public SmartRecruiters company postings connector; private/partner APIs remain disabled."""

    name = "smartrecruiters"
    kind = "ats"
    default_mode = "credential_required"

    def __init__(
        self,
        settings: Any,
        *,
        company_ids: list[str] | None = None,
        use_live_network: bool = False,
        http_client: Any = None,
    ) -> None:
        super().__init__(settings, use_live_network=use_live_network, http_client=http_client)
        self.company_ids = company_ids or []

    def parse_payload(self, payload: dict[str, Any], company_id: str) -> list[NormalizedJob]:
        jobs: list[NormalizedJob] = []
        now = datetime.now(UTC)
        for item in payload.get("content") or []:
            if not isinstance(item, dict):
                continue
            jid = str(item.get("id") or "")
            title = str(item.get("name") or "").strip()
            ref = str(item.get("ref") or f"https://jobs.smartrecruiters.com/{company_id}/{jid}").strip()
            if not title:
                continue
            raw_loc = item.get("location")
            loc_obj: dict[str, Any] = raw_loc if isinstance(raw_loc, dict) else {}
            city = loc_obj.get("city")
            country = loc_obj.get("country")
            loc = ", ".join([str(p) for p in (city, country) if p]) or None
            comp_name = ((item.get("company") or {}).get("name") if isinstance(item.get("company"), dict) else None) or company_id
            try:
                jobs.append(
                    NormalizedJob(
                        source=self.name,
                        source_job_id=jid or None,
                        company=str(comp_name),
                        title=title,
                        location=loc,
                        workplace_type=infer_workplace_type(
                            f"{title} {loc or ''}",
                            is_remote=bool(loc_obj.get("remote")),
                        ),
                        description=self._clean_description(str(item.get("jobAd", {}).get("sections", {}).get("jobDescription", {}).get("text") or title)),
                        job_url=ref,
                        apply_url=ref,
                        posted_at=parse_timestamp(item.get("releasedDate")),
                        discovered_at=now,
                        seniority=infer_seniority(title),
                        apply_mode="assisted_manual_submit",
                        source_attribution=f"SmartRecruiters ({comp_name})",
                    )
                )
            except Exception:
                continue
        return jobs

    async def discover(self, query: SearchQuery) -> list[NormalizedJob]:
        if not self.use_live_network and self._http_client is None:
            return []
        results: list[NormalizedJob] = []
        for cid in self.company_ids[:3]:
            url = f"https://api.smartrecruiters.com/v1/companies/{cid}/postings"
            payload = await self._get_json(url)
            if isinstance(payload, dict):
                results.extend(self.parse_payload(payload, cid))
        return results[: query.limit]
