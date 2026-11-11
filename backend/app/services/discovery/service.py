"""Job discovery orchestration, canonical URL normalization, cross-site dedup, and connector health."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.connectors.ats.ashby import AshbyConnector
from backend.app.connectors.ats.fictional_test_ats import FictionalTestAtsConnector
from backend.app.connectors.ats.greenhouse import GreenhouseConnector
from backend.app.connectors.ats.lever import LeverConnector
from backend.app.connectors.ats.smartrecruiters import SmartRecruitersConnector
from backend.app.connectors.base import JobSource
from backend.app.connectors.job_boards.arbeitnow import ArbeitnowConnector
from backend.app.connectors.job_boards.himalayas import HimalayasConnector
from backend.app.connectors.job_boards.manual_boards import ManualBoardAlertsConnector
from backend.app.connectors.job_boards.remotive import RemotiveConnector
from backend.app.connectors.search.brave import BraveSearchConnector
from backend.app.connectors.search.tavily_adzuna import AdzunaConnector, TavilySearchConnector
from backend.app.core.config import Settings
from backend.app.models.tables import (
    AuditEvent,
    Employer,
    JobPosting,
    JobRequirement,
    JobSourceRecord,
    SourceConnector,
)
from backend.app.schemas.contracts import NormalizedJob, SearchQuery
from backend.app.security.prompt_guard import sanitize_untrusted_text
from backend.app.security.redaction import redact_secrets

TRACKING_PARAMS: frozenset[str] = frozenset(
    {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "gh_src", "ref", "source"}
)

KNOWN_SKILLS: tuple[str, ...] = (
    "python",
    "sql",
    "fastapi",
    "etl",
    "databricks",
    "azure",
    "power bi",
    "pandas",
    "pytest",
    "sqlalchemy",
    "git",
    "docker",
    "machine learning",
    "dbt",
    "spark",
    "airflow",
    "aws",
    "gcp",
    "kubernetes",
    "rust",
)


def canonicalize_url(url: str) -> str:
    """Strip tracking query params and fragments for stable canonical deduplication."""
    parsed = urlparse(url.strip())
    kept_q = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ]
    clean_path = parsed.path.rstrip("/") or "/"
    return urlunparse(
        (
            parsed.scheme.lower(),
            (parsed.netloc or "").lower(),
            clean_path,
            "",
            urlencode(kept_q),
            "",
        )
    )


def compute_dedup_signature(company: str, title: str) -> str:
    """Build normalized company+title fingerprint for cross-board duplicate detection."""
    def _norm(txt: str) -> str:
        t = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", txt.lower())
        t = re.sub(r"[^a-z0-9áéíóúâêôãõç]+", " ", t)
        return " ".join(t.split())

    raw = f"{_norm(company)}::{_norm(title)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def description_similarity(a: str, b: str) -> float:
    """Token Jaccard similarity used alongside company+title signature for cross-site dedup."""
    ta = set(re.findall(r"[a-z0-9]{3,}", a.lower()))
    tb = set(re.findall(r"[a-z0-9]{3,}", b.lower()))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def extract_requirements(description: str) -> list[tuple[str, str, str]]:
    """Distinguish required vs preferred skill requirements from job descriptions."""
    reqs: list[tuple[str, str, str]] = []
    sentences = re.split(r"(?<=[.!?\n])\s+", description)
    seen: set[tuple[str, str]] = set()
    for sent in sentences:
        low_s = sent.lower()
        is_pref = any(
            k in low_s
            for k in ("desejável", "desejavel", "diferencial", "preferred", "nice to have", "plus")
        )
        kind = "preferred" if is_pref else "required"
        for skill in KNOWN_SKILLS:
            if re.search(rf"\b{re.escape(skill)}\b", low_s):
                key = (kind, skill)
                if key not in seen:
                    seen.add(key)
                    reqs.append((kind, skill, sent[:380]))
    return reqs


def configured_boards(value: str) -> list[str]:
    return list(dict.fromkeys(
        slug.strip() for slug in value.split(",") if re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", slug.strip())
    ))[:5]


def live_source_names(settings: Settings) -> list[str]:
    sources = ["arbeitnow", "himalayas", "remotive"]
    if configured_boards(settings.greenhouse_boards):
        sources.append("greenhouse")
    if configured_boards(settings.ashby_boards):
        sources.append("ashby")
    if configured_boards(settings.lever_boards):
        sources.append("lever")
    if settings.brave_search_api_key.strip():
        sources.append("brave_search")
    return sources


def matches_search(job: NormalizedJob, query: SearchQuery) -> bool:
    def words(text: str) -> set[str]:
        return set(re.findall(r"[\w+#]+", text.casefold()))

    terms = words(query.text) - {"de", "da", "do", "e", "a", "the", "and", "vaga", "vagas", "jobs"}
    text = words(f"{job.title} {job.description} {job.company}")
    if terms and len(text & terms) < (len(terms) + 1) // 2:
        return False
    remote = job.workplace_type == "remote"
    if query.remote_only and not remote:
        return False
    location = words(query.location or "")
    if not location:
        return True
    return location <= words(job.location or "") or (query.include_remote and remote)


def build_default_connectors(
    settings: Settings, *, use_live_network: bool = False
) -> list[JobSource]:
    """Instantiate all supported compliant connectors with isolated configuration."""
    return [
        FictionalTestAtsConnector(settings, use_live_network=use_live_network),
        GreenhouseConnector(settings, board_tokens=configured_boards(settings.greenhouse_boards), use_live_network=use_live_network),
        AshbyConnector(settings, board_names=configured_boards(settings.ashby_boards), use_live_network=use_live_network),
        LeverConnector(settings, sites=[(site, "global") for site in configured_boards(settings.lever_boards)], use_live_network=use_live_network),
        HimalayasConnector(settings, use_live_network=use_live_network),
        RemotiveConnector(settings, use_live_network=use_live_network),
        ArbeitnowConnector(settings, use_live_network=use_live_network),
        ManualBoardAlertsConnector(settings, use_live_network=use_live_network),
        SmartRecruitersConnector(settings, use_live_network=use_live_network),
        BraveSearchConnector(settings, use_live_network=use_live_network),
        TavilySearchConnector(settings, use_live_network=use_live_network),
        AdzunaConnector(settings, use_live_network=use_live_network),
    ]


def ensure_connectors_seeded(db: Session, settings: Settings) -> list[SourceConnector]:
    """Ensure every connector exists in `source_connectors` with accurate mode metadata."""
    existing = {c.name: c for c in db.scalars(select(SourceConnector)).all()}
    for conn in build_default_connectors(settings):
        if conn.name not in existing:
            mode = conn.default_mode
            enabled = conn.name not in {"smartrecruiters", "brave_search", "tavily_search", "adzuna"}
            if conn.name == "brave_search" and settings.brave_search_api_key.strip():
                mode, enabled = "live", True
            if conn.name == "tavily_search" and settings.tavily_api_key.strip():
                mode, enabled = "live", True
            if conn.name == "adzuna" and settings.adzuna_app_id.strip() and settings.adzuna_app_key.strip():
                mode, enabled = "live", True
            row = SourceConnector(
                name=conn.name,
                kind=conn.kind,
                enabled=enabled,
                mode=mode,
                last_status="idle",
            )
            db.add(row)
            existing[conn.name] = row
    db.flush()
    return list(existing.values())


class DiscoveryService:
    """Runs connectors with error isolation, normalizes records, deduplicates, and persists."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def run_discovery(
        self,
        db: Session,
        query: SearchQuery,
        *,
        sources: list[str] | None = None,
        use_live_network: bool = False,
        connectors_override: list[JobSource] | None = None,
    ) -> dict[str, object]:
        connector_rows = {c.name: c for c in ensure_connectors_seeded(db, self.settings)}
        connectors = connectors_override or build_default_connectors(
            self.settings, use_live_network=use_live_network
        )
        selected_set = set(sources) if sources else None

        if use_live_network and connectors_override is None and sources is None:
            selected_set = set(live_source_names(self.settings))

        total_raw = 0
        total_inserted = 0
        total_deduped = 0
        errors: dict[str, str] = {}
        active_sources: list[str] = []
        matched_ids: set[str] = set()

        for conn in connectors:
            row = connector_rows.get(conn.name)
            if selected_set is not None and conn.name not in selected_set:
                continue
            if row is not None and not row.enabled and connectors_override is None:
                continue

            active_sources.append(conn.name)
            try:
                discovered = await conn.discover(query)
                total_raw += len(discovered)
                if use_live_network and connectors_override is None:
                    discovered = [job for job in discovered if matches_search(job, query)]
                inserted, deduped = self._persist_jobs(
                    db, discovered, is_mocked=not use_live_network or conn.name in {"fictional_ats", "manual_alerts"},
                    matched_ids=matched_ids,
                )
                total_inserted += inserted
                total_deduped += deduped
                if row is not None:
                    row.last_status = "ok"
                    row.last_error = None
                    row.last_checked_at = datetime.now(UTC)
                    row.total_fetched += len(discovered)
            except Exception as exc:
                errors[conn.name] = redact_secrets(str(exc), self.settings.secret_values())[:500]
                if row is not None:
                    row.last_status = "error"
                    row.last_error = errors[conn.name]
                    row.last_checked_at = datetime.now(UTC)

        db.add(
            AuditEvent(
                actor="system:discovery",
                event_type="discovery_completed",
                entity_type="search",
                entity_id="batch",
                details={
                    "query": query.text,
                    "sources": active_sources,
                    "total_raw": total_raw,
                    "inserted": total_inserted,
                    "deduped": total_deduped,
                    "errors": errors,
                },
            )
        )
        db.commit()
        return {
            "total_raw": total_raw,
            "inserted": total_inserted,
            "deduped": total_deduped,
            "sources_run": active_sources,
            "job_ids": sorted(matched_ids),
            "errors": errors,
        }

    def _persist_jobs(
        self, db: Session, items: list[NormalizedJob], *, is_mocked: bool, matched_ids: set[str]
    ) -> tuple[int, int]:
        inserted = 0
        deduped = 0
        expiry_cutoff = datetime.now(UTC) - timedelta(days=60)

        for item in items:
            canon_url = canonicalize_url(item.job_url)
            sig = compute_dedup_signature(item.company, item.title)
            sanitized = sanitize_untrusted_text(item.description)
            desc_hash = hashlib.sha256(sanitized.text.encode("utf-8")).hexdigest()

            existing = db.scalar(select(JobPosting).where(JobPosting.canonical_url == canon_url))
            if existing is None:
                # Cross-site duplicate check by signature + text similarity
                candidates = db.scalars(
                    select(JobPosting).where(
                        JobPosting.dedup_signature == sig,
                        select(JobSourceRecord.id).where(
                            JobSourceRecord.job_id == JobPosting.id,
                            JobSourceRecord.is_mocked == is_mocked,
                        ).exists(),
                    )
                ).all()
                for cand in candidates:
                    if description_similarity(cand.description, sanitized.text) >= 0.65:
                        existing = cand
                        break

            provider_id = item.source_job_id or canon_url
            if existing is not None:
                deduped += 1
                # Attach provenance record if this provider has not yet been linked
                src_rec = db.scalar(
                    select(JobSourceRecord).where(
                        JobSourceRecord.source == item.source,
                        JobSourceRecord.provider_job_id == provider_id,
                    )
                )
                if src_rec is not None and src_rec.is_mocked != is_mocked:
                    # A provider ID already backed by fixtures cannot be promoted to live data.
                    continue
                matched_ids.add(existing.id)
                if src_rec is None:
                    db.add(
                        JobSourceRecord(
                            job_id=existing.id,
                            source=item.source,
                            provider_job_id=provider_id,
                            raw_url=item.job_url,
                            attribution=item.source_attribution,
                            is_mocked=is_mocked,
                        )
                    )
                    db.flush()
                continue

            employer = db.scalar(select(Employer).where(Employer.name == item.company))
            if employer is None:
                employer = Employer(
                    name=item.company,
                    ats_platform=item.source,
                    is_fictional_test=(item.source == "fictional_ats" or "fictíci" in item.company.lower()),
                )
                db.add(employer)
                db.flush()

            is_expired = bool(item.posted_at and item.posted_at.replace(tzinfo=UTC) < expiry_cutoff)
            job = JobPosting(
                employer_id=employer.id,
                canonical_url=canon_url,
                dedup_signature=sig,
                source=item.source,
                source_job_id=provider_id,
                company=item.company,
                title=item.title,
                location=item.location,
                workplace_type=item.workplace_type,
                description=sanitized.text,
                description_hash=desc_hash,
                job_url=item.job_url,
                apply_url=item.apply_url,
                posted_at=item.posted_at,
                discovered_at=item.discovered_at,
                is_expired=is_expired,
                employment_type=item.employment_type,
                seniority=item.seniority,
                salary_min=item.salary_min,
                salary_max=item.salary_max,
                salary_currency=item.salary_currency,
                apply_mode=item.apply_mode,
                source_attribution=item.source_attribution,
                injection_signals=sanitized.injection_signals,
            )
            db.add(job)
            db.flush()

            db.add(
                JobSourceRecord(
                    job_id=job.id,
                    source=item.source,
                    provider_job_id=provider_id,
                    raw_url=item.job_url,
                    attribution=item.source_attribution,
                    is_mocked=is_mocked,
                )
            )
            for kind, term, snippet in extract_requirements(sanitized.text):
                db.add(JobRequirement(job_id=job.id, kind=kind, term=term, snippet=snippet))

            db.flush()
            matched_ids.add(job.id)
            inserted += 1
        return inserted, deduped
