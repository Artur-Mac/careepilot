"""Parser for public schema.org/JobPosting JSON-LD blocks on permitted company career pages."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from bs4 import BeautifulSoup

from backend.app.connectors.base import infer_seniority, infer_workplace_type, parse_timestamp
from backend.app.core.ssrf import validate_external_url
from backend.app.schemas.contracts import NormalizedJob
from backend.app.security.prompt_guard import sanitize_untrusted_text


def extract_job_postings_from_jsonld(
    html_text: str,
    page_url: str,
    *,
    source_name: str = "company_jsonld",
    allow_test_origins: set[str] | None = None,
) -> list[NormalizedJob]:
    """Parse schema.org/JobPosting JSON-LD from a career page safely with injection sanitization."""
    soup = BeautifulSoup(html_text, "html.parser")
    results: list[NormalizedJob] = []
    now = datetime.now(UTC)

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        if not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue

        items: list[dict[str, Any]] = []
        if isinstance(parsed, dict):
            if "@graph" in parsed and isinstance(parsed["@graph"], list):
                items.extend([x for x in parsed["@graph"] if isinstance(x, dict)])
            else:
                items.append(parsed)
        elif isinstance(parsed, list):
            items.extend([x for x in parsed if isinstance(x, dict)])

        for obj in items:
            jtype = str(obj.get("@type") or "")
            if "JobPosting" not in jtype:
                continue
            title = str(obj.get("title") or "").strip()
            if not title:
                continue
            org = obj.get("hiringOrganization")
            company = str(org.get("name")) if isinstance(org, dict) and org.get("name") else "Empresa"
            desc = sanitize_untrusted_text(str(obj.get("description") or title)).text
            loc_raw = obj.get("jobLocation")
            location: str | None = None
            if isinstance(loc_raw, dict):
                addr = loc_raw.get("address") or {}
                if isinstance(addr, dict):
                    parts = [addr.get("addressLocality"), addr.get("addressRegion"), addr.get("addressCountry")]
                    location = ", ".join(str(p) for p in parts if p) or None
            job_loc_type = str(obj.get("jobLocationType") or "")
            wp = infer_workplace_type(
                f"{job_loc_type} {location or ''} {title}",
                is_remote="TELECOMMUTE" in job_loc_type.upper(),
            )
            job_url = str(obj.get("url") or page_url)
            try:
                safe_url = validate_external_url(job_url, allow_test_origins=allow_test_origins)
                job_id = str(
                    obj.get("identifier", {}).get("value")
                    if isinstance(obj.get("identifier"), dict)
                    else safe_url
                )
                payload = {
                    "source": source_name,
                    "source_job_id": job_id,
                    "company": company,
                    "title": title,
                    "location": location,
                    "workplace_type": wp,
                    "description": desc,
                    "job_url": safe_url,
                    "apply_url": safe_url,
                    "posted_at": parse_timestamp(obj.get("datePosted")),
                    "discovered_at": now,
                    "employment_type": str(obj.get("employmentType") or "") or None,
                    "seniority": infer_seniority(title),
                    "source_attribution": f"Página de carreiras ({company}) via JSON-LD",
                    "discovery_channel": "browser",
                }
                ctx = {"allow_test_origins": allow_test_origins} if allow_test_origins else None
                results.append(NormalizedJob.model_validate(payload, context=ctx))
            except Exception:
                continue
    return results
