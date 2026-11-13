"""Browser-based job discovery orchestration, 3-tier dedup, geography filter, and async DiscoveryRun."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.browser_automation.session import BrowserSession, BrowserUnavailableError
from backend.app.connectors.browser import (
    ENABLED_BROWSER_SOURCES,
    BrowserCollector,
    BrowserDiscoveryPlan,
    build_browser_collectors,
)
from backend.app.connectors.search.query_generator import generate_portal_queries
from backend.app.core.config import Settings
from backend.app.db.session import get_session_factory
from backend.app.models.tables import (
    AuditEvent,
    DiscoveryRun,
    Employer,
    JobDismissal,
    JobPosting,
    JobSourceRecord,
    SourceConnector,
)
from backend.app.schemas.contracts import NormalizedJob
from backend.app.security.prompt_guard import sanitize_untrusted_text
from backend.app.security.redaction import redact_secrets
from backend.app.services.discovery.eligibility import classify_job_eligibility
from backend.app.services.profile.service import ProfileService

TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "gh_src",
        "ref",
        "source",
        "trackingid",
        "refid",
        "ebp",
        "trk",
        "trkinfo",
        "recommendedflavor",
        "gclid",
        "fbclid",
    }
)


class ActiveDiscoveryConflictError(RuntimeError):
    """Raised when starting a discovery run while another run is active."""


class MissingDiscoveryRoleError(ValueError):
    """Raised when neither MasterProfile nor the request specifies a target role."""


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


def encode_jobs_cursor(discovered_at: datetime, job_id: str) -> str:
    """Encode a keyset cursor `(discovered_at, id)` as URL-safe base64 JSON."""
    dt = discovered_at if discovered_at.tzinfo else discovered_at.replace(tzinfo=UTC)
    payload = json.dumps({"ts": dt.astimezone(UTC).isoformat(), "id": job_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def decode_jobs_cursor(cursor: str) -> tuple[datetime, str]:
    """Decode and validate a keyset cursor `(discovered_at, id)`."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("ts"), str) or not isinstance(data.get("id"), str):
            raise ValueError("Cursor malformado.")
        dt = datetime.fromisoformat(data["ts"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        job_id = data["id"].strip()
        if not job_id or len(job_id) > 64:
            raise ValueError("ID de cursor inválido.")
        return dt.astimezone(UTC), job_id
    except Exception as exc:
        raise ValueError("Cursor de paginação inválido.") from exc


def ensure_connectors_seeded(
    db: Session, _settings: Settings | None = None
) -> list[SourceConnector]:
    """Ensure browser collectors exist in `source_connectors` without legacy API sources."""
    existing = {c.name: c for c in db.scalars(select(SourceConnector)).all()}
    stale_names = [name for name in existing if name not in ENABLED_BROWSER_SOURCES]
    for name in stale_names:
        db.delete(existing.pop(name))

    for source_name in ENABLED_BROWSER_SOURCES:
        if source_name not in existing:
            row = SourceConnector(
                name=source_name,
                kind="browser",
                enabled=True,
                mode="browser",
                rate_limit_rpm=20,
                last_status="idle",
            )
            db.add(row)
            existing[source_name] = row
    db.flush()
    return [existing[name] for name in ENABLED_BROWSER_SOURCES if name in existing]


def serialize_discovery_run(run: DiscoveryRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "status": run.status,
        "query_text": run.query_text,
        "queries": list(run.queries_json or []),
        "sources": list(run.sources_json or []),
        "source_errors": dict(run.source_errors_json or {}),
        "total_raw": run.total_raw,
        "inserted_count": run.inserted_count,
        "deduped_count": run.deduped_count,
        "rejected_restricted_count": run.rejected_restricted_count,
        "matched_job_ids": list(run.matched_job_ids_json or []),
        "error": run.error,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
    }


def recover_stale_discovery_runs(
    db: Session, *, reason: str = "Busca interrompida pelo encerramento do servidor."
) -> int:
    """Mark any left-over `running` DiscoveryRun rows as `failed` cleanly."""
    running = db.scalars(select(DiscoveryRun).where(DiscoveryRun.status == "running")).all()
    if not running:
        return 0
    now = datetime.now(UTC)
    for row in running:
        row.status = "failed"
        row.error = reason
        row.finished_at = now
    db.commit()
    return len(running)


class DiscoveryService:
    """Orchestrates browser collectors with 3-tier deduplication and geography eligibility."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def resolve_discovery_plan(
        self,
        db: Session,
        *,
        user_id: str = "local-applicant",
        role_override: str | None = None,
        location: str | None = None,
        remote_only: bool = False,
        include_remote: bool = True,
        limit_per_source: int = 15,
    ) -> BrowserDiscoveryPlan:
        """Build a discovery plan from the user's MasterProfile or explicit role_override."""
        doc, _rev = ProfileService().get_document(db, user_id=user_id)
        override = (role_override or "").strip()
        roles = [override] if override else [r.strip() for r in doc.preferences.target_roles if r.strip()]
        if not roles:
            raise MissingDiscoveryRoleError(
                "Defina ao menos um cargo alvo no Perfil mestre ou informe um cargo para buscar."
            )

        pref_loc = (
            location.strip()
            if location is not None and location.strip()
            else (doc.preferences.location or ("" if remote_only else "Recife, PE"))
        )
        return BrowserDiscoveryPlan(
            roles=roles[:4],
            location=pref_loc or None,
            remote_only=remote_only,
            include_remote=include_remote,
            limit_per_source=limit_per_source,
        )

    def create_discovery_run(
        self,
        db: Session,
        *,
        user_id: str,
        plan: BrowserDiscoveryPlan,
        sources: list[str] | None = None,
    ) -> DiscoveryRun:
        """Create a new DiscoveryRun row or raise 409 conflict if one is already running."""
        active = db.scalar(
            select(DiscoveryRun).where(
                DiscoveryRun.user_id == user_id,
                DiscoveryRun.status == "running",
            )
        )
        if active is not None:
            raise ActiveDiscoveryConflictError("Já existe uma busca de vagas em andamento.")

        ensure_connectors_seeded(db)
        selected_sources = [
            s for s in ENABLED_BROWSER_SOURCES if sources is None or s in set(sources)
        ] or list(ENABLED_BROWSER_SOURCES)

        portal_queries = generate_portal_queries(
            plan.roles,
            location=plan.location,
            remote_only=plan.remote_only,
            include_remote=plan.include_remote,
            max_queries=4,
        )
        query_strings = [f"{q['keywords']} ({q['location']})" for q in portal_queries]
        run = DiscoveryRun(
            user_id=user_id,
            status="running",
            query_text=f"{', '.join(plan.roles)} · {plan.location or 'Remoto'}"[:300],
            queries_json=query_strings,
            sources_json=selected_sources,
            source_errors_json={},
            matched_job_ids_json=[],
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        return run

    async def execute_run(
        self,
        run_id: str,
        *,
        session: BrowserSession,
        plan: BrowserDiscoveryPlan,
        sources: list[str] | None = None,
        collectors_override: list[BrowserCollector] | None = None,
    ) -> None:
        """Execute a background DiscoveryRun in its own DB session and close discovery tabs cleanly."""
        sf = get_session_factory(self.settings)
        with sf() as db:
            run = db.get(DiscoveryRun, run_id)
            if run is None:
                return
            await self.run_collectors_for_existing_run(
                db,
                run=run,
                session=session,
                plan=plan,
                sources=sources,
                collectors_override=collectors_override,
            )

    async def run_collectors_for_existing_run(
        self,
        db: Session,
        *,
        run: DiscoveryRun,
        session: BrowserSession,
        plan: BrowserDiscoveryPlan,
        sources: list[str] | None = None,
        collectors_override: list[BrowserCollector] | None = None,
    ) -> DiscoveryRun:
        """Run selected browser collectors, isolate portal errors, deduplicate, and finalize DiscoveryRun."""
        connector_rows = {c.name: c for c in ensure_connectors_seeded(db)}
        collectors = (
            list(collectors_override)
            if collectors_override is not None
            else build_browser_collectors()
        )
        selected_set = set(sources) if sources else None

        total_raw = 0
        total_inserted = 0
        total_deduped = 0
        total_rejected = 0
        source_errors: dict[str, str] = {}
        active_sources: list[str] = []
        matched_ids: set[str] = set()
        browser_unavailable_error: str | None = None

        try:
            for coll in collectors:
                if selected_set is not None and coll.name not in selected_set:
                    continue
                row = connector_rows.get(coll.name)
                if row is not None and not row.enabled and collectors_override is None:
                    continue

                active_sources.append(coll.name)
                try:
                    discovered = await coll.collect(session, plan)
                    total_raw += len(discovered)
                    inserted, deduped, rejected = self.persist_items(
                        db, discovered, matched_ids=matched_ids
                    )
                    total_inserted += inserted
                    total_deduped += deduped
                    total_rejected += rejected
                    if row is not None:
                        row.last_status = "ok"
                        row.last_error = None
                        row.last_checked_at = datetime.now(UTC)
                        row.total_fetched += len(discovered)
                except BrowserUnavailableError as exc:
                    msg = redact_secrets(str(exc), self.settings.secret_values())[:500]
                    browser_unavailable_error = msg
                    source_errors[coll.name] = msg
                    if row is not None:
                        row.last_status = "error"
                        row.last_error = msg
                        row.last_checked_at = datetime.now(UTC)
                    # Same local CDP browser is down for remaining browser collectors too
                    break
                except Exception as exc:
                    msg = redact_secrets(str(exc), self.settings.secret_values())[:500]
                    source_errors[coll.name] = msg
                    if row is not None:
                        row.last_status = "error"
                        row.last_error = msg
                        row.last_checked_at = datetime.now(UTC)

            run.sources_json = active_sources
            run.source_errors_json = source_errors
            run.total_raw = total_raw
            run.inserted_count = total_inserted
            run.deduped_count = total_deduped
            run.rejected_restricted_count = total_rejected
            run.matched_job_ids_json = sorted(matched_ids)
            run.finished_at = datetime.now(UTC)
            if browser_unavailable_error and total_raw == 0:
                run.status = "failed"
                run.error = browser_unavailable_error
            elif active_sources and len(source_errors) == len(active_sources) and total_raw == 0:
                run.status = "failed"
                run.error = "Nenhuma fonte retornou resultados; verifique o login no Chrome."
            else:
                run.status = "completed"
                run.error = None

            db.add(
                AuditEvent(
                    actor="system:discovery",
                    event_type="discovery_completed",
                    entity_type="discovery_run",
                    entity_id=run.id,
                    details={
                        "query": run.query_text,
                        "sources": active_sources,
                        "total_raw": total_raw,
                        "inserted": total_inserted,
                        "deduped": total_deduped,
                        "rejected_restricted": total_rejected,
                        "errors": source_errors,
                    },
                )
            )
            db.commit()
            db.refresh(run)
            return run
        finally:
            with suppress(Exception):
                await session.close_discovery_tabs()

    def persist_items(
        self,
        db: Session,
        items: list[NormalizedJob],
        *,
        matched_ids: set[str],
    ) -> tuple[int, int, int]:
        """Persist normalized jobs with 3-tier deduplication and geography eligibility."""
        inserted = 0
        deduped = 0
        rejected_restricted = 0
        expiry_cutoff = datetime.now(UTC) - timedelta(days=60)

        for item in items:
            canon_url = canonicalize_url(item.job_url)
            provider_id = item.source_job_id or canon_url
            sig = compute_dedup_signature(item.company, item.title)
            sanitized = sanitize_untrusted_text(item.description)
            desc_hash = hashlib.sha256(sanitized.text.encode("utf-8")).hexdigest()

            # Tier 1: (source, provider_job_id)
            src_rec = db.scalar(
                select(JobSourceRecord).where(
                    JobSourceRecord.source == item.source,
                    JobSourceRecord.provider_job_id == provider_id,
                )
            )
            existing: JobPosting | None = db.get(JobPosting, src_rec.job_id) if src_rec else None

            # Tier 2: canonical_url
            if existing is None:
                existing = db.scalar(
                    select(JobPosting).where(JobPosting.canonical_url == canon_url)
                )

            # Tier 3: dedup_signature + description similarity
            if existing is None:
                candidates = db.scalars(
                    select(JobPosting).where(JobPosting.dedup_signature == sig)
                ).all()
                for cand in candidates:
                    if description_similarity(cand.description, sanitized.text) >= 0.65:
                        existing = cand
                        break

            if existing is not None:
                deduped += 1
                is_restricted = (existing.eligibility_json or {}).get("brazil_eligible") == "restricted"
                if not is_restricted:
                    matched_ids.add(existing.id)
                if src_rec is None:
                    db.add(
                        JobSourceRecord(
                            job_id=existing.id,
                            source=item.source,
                            provider_job_id=provider_id,
                            raw_url=item.job_url,
                            attribution=item.source_attribution,
                            discovery_channel=item.discovery_channel,
                        )
                    )
                    db.flush()
                continue

            eligibility = classify_job_eligibility(
                title=item.title,
                location=item.location,
                workplace_type=item.workplace_type,
                description=sanitized.text,
            )

            employer = db.scalar(select(Employer).where(Employer.name == item.company))
            if employer is None:
                employer = Employer(name=item.company, ats_platform=item.source)
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
                summary_text=None,
                eligibility_json=eligibility,
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
                    discovery_channel=item.discovery_channel,
                )
            )
            db.flush()

            if eligibility["brazil_eligible"] == "restricted":
                rejected_restricted += 1
            else:
                matched_ids.add(job.id)
                inserted += 1

        return inserted, deduped, rejected_restricted

    def dismiss_job(self, db: Session, *, user_id: str, job_id: str) -> dict[str, Any]:
        """Hide a job permanently from the user's active Vagas feed."""
        job = db.get(JobPosting, job_id)
        if job is None:
            raise KeyError("Vaga não encontrada.")
        existing = db.scalar(
            select(JobDismissal).where(
                JobDismissal.user_id == user_id,
                JobDismissal.job_id == job_id,
            )
        )
        if existing is None:
            db.add(JobDismissal(user_id=user_id, job_id=job_id))
            db.add(
                AuditEvent(
                    actor=user_id,
                    event_type="job.dismissed",
                    entity_type="job_posting",
                    entity_id=job_id,
                    details={"title": job.title, "company": job.company},
                )
            )
            db.commit()
        return {"job_id": job_id, "dismissed": True}
