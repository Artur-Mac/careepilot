"""Application preparation, artifact reuse, listing, and local completion service."""

from __future__ import annotations

import asyncio
import threading
import weakref
from pathlib import Path
from typing import Any

from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.config import Settings
from backend.app.core.state_machine import (
    ACTIVE_PANEL_STATUSES,
    RESUMABLE_STATUSES,
    ApplicationStatus,
)
from backend.app.db.session import get_session_factory
from backend.app.models.tables import Application, AuditEvent, JobPosting, ResumeVersion
from backend.app.security.redaction import redact_secrets
from backend.app.services.profile.service import ProfileService
from backend.app.services.resume.service import (
    MissingProfileContentError,
    ResumeService,
    resolve_safe_resume_path,
)

_LOOP_LOCKS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[tuple[str, str, str], asyncio.Lock]
] = weakref.WeakKeyDictionary()
_LOCKS_GUARD = threading.Lock()


def _get_prepare_lock(database_url: str, user_id: str, job_id: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    with _LOCKS_GUARD:
        per_loop = _LOOP_LOCKS.get(loop)
        if per_loop is None:
            per_loop = {}
            _LOOP_LOCKS[loop] = per_loop
        key = (database_url, user_id, job_id)
        lock = per_loop.get(key)
        if lock is None:
            lock = asyncio.Lock()
            per_loop[key] = lock
        return lock


class JobNotFoundError(LookupError):
    """Raised when the requested job posting does not exist."""


class ApplicationNotFoundError(LookupError):
    """Raised when the requested application does not exist or is not owned by the user."""


class PrepareConflictError(RuntimeError):
    """Raised when profile revision or job description changes mid-prepare before publish."""


class ApplicationService:
    """Coordinates lock-serialized résumé tailoring, reuse, and local completion."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.profile_service = ProfileService()
        self.resume_service = ResumeService(settings)

    def _is_reusable_resume(
        self,
        db: Session,
        *,
        resume_id: str | None,
        user_id: str,
        profile_rev: int,
        job_desc_hash: str,
    ) -> ResumeVersion | None:
        if not resume_id:
            return None
        rv = db.scalar(select(ResumeVersion).where(ResumeVersion.id == resume_id))
        if rv is None or rv.user_id != user_id:
            return None
        if rv.profile_revision != profile_rev or rv.job_description_hash != job_desc_hash:
            return None
        try:
            path = resolve_safe_resume_path(self.settings, rv, user_id=user_id)
            reader = PdfReader(str(path))
            if not reader.pages:
                return None
        except Exception:
            return None
        return rv

    def _serialize_application(
        self,
        app: Application,
        job: JobPosting | None,
        rv: ResumeVersion | None,
        *,
        current_profile_revision: int | None = None,
    ) -> dict[str, Any]:
        resume_profile_rev = rv.profile_revision if rv else None
        stale_profile = bool(
            rv is not None
            and current_profile_revision is not None
            and resume_profile_rev is not None
            and resume_profile_rev != current_profile_revision
        )
        return {
            "id": app.id,
            "job_id": app.job_id,
            "status": app.status,
            "resume_id": app.resume_version_id,
            "last_step": app.last_step_json,
            "updated_at": app.updated_at.isoformat() if app.updated_at else None,
            "created_at": app.created_at.isoformat() if app.created_at else None,
            "last_error": app.last_error,
            "job_title": job.title if job else None,
            "company": job.company if job else None,
            "job_url": (job.apply_url or job.job_url) if job else None,
            "location": job.location if job else None,
            "workplace_type": job.workplace_type if job else None,
            "tailoring_mode": (rv.diff_summary or {}).get("mode") if rv else None,
            "resume_profile_revision": resume_profile_rev,
            "profile_revision_stale": stale_profile,
        }

    async def prepare_application(
        self,
        *,
        job_id: str,
        user_id: str = "local-applicant",
    ) -> dict[str, Any]:
        """Prepare or reuse a truthful PDF and resumable Application under a per-(user, job) lock."""
        lock = _get_prepare_lock(self.settings.database_url, user_id, job_id)
        session_factory = get_session_factory(self.settings)

        async with lock:
            # 1. Short read session — gather current state and release connection before async I/O.
            with session_factory() as read_db:
                job = read_db.scalar(select(JobPosting).where(JobPosting.id == job_id))
                if job is None:
                    raise JobNotFoundError("Vaga não encontrada.")

                apps = list(
                    read_db.scalars(
                        select(Application)
                        .where(Application.job_id == job_id, Application.user_id == user_id)
                        .order_by(Application.updated_at.desc(), Application.created_at.desc(), Application.id.desc())
                    ).all()
                )
                non_closed = [a for a in apps if a.status != ApplicationStatus.CLOSED.value]
                if non_closed and non_closed[0].status == ApplicationStatus.DONE.value:
                    done_app = non_closed[0]
                    done_rv = (
                        read_db.scalar(
                            select(ResumeVersion).where(ResumeVersion.id == done_app.resume_version_id)
                        )
                        if done_app.resume_version_id
                        else None
                    )
                    _, current_rev = self.profile_service.get_document(read_db, user_id=user_id)
                    return {
                        "application": self._serialize_application(
                            done_app, job, done_rv, current_profile_revision=current_rev
                        ),
                        "reused_pdf": True,
                        "already_done": True,
                    }

                existing_resumable = next(
                    (a for a in non_closed if a.status in RESUMABLE_STATUSES),
                    None,
                )

                doc, profile_rev = self.profile_service.get_document(read_db, user_id=user_id)
                if not doc.has_resume_content():
                    raise MissingProfileContentError(
                        "Preencha nome completo e ao menos experiência, formação, projeto, skill ou currículo importado no Perfil mestre."
                    )

                if existing_resumable is not None:
                    reusable_rv = self._is_reusable_resume(
                        read_db,
                        resume_id=existing_resumable.resume_version_id,
                        user_id=user_id,
                        profile_rev=profile_rev,
                        job_desc_hash=job.description_hash,
                    )
                    if reusable_rv is not None:
                        return {
                            "application": self._serialize_application(
                                existing_resumable,
                                job,
                                reusable_rv,
                                current_profile_revision=profile_rev,
                            ),
                            "reused_pdf": True,
                            "already_done": False,
                        }

                remaining_budget = self.resume_service.llm.remaining_budget_usd(read_db)
                job_title = job.title
                job_company = job.company
                job_location = job.location
                job_workplace = job.workplace_type
                job_source = job.source
                job_description = job.description
                job_desc_hash = job.description_hash
                existing_app_id = existing_resumable.id if existing_resumable else None

            # 2. External model + CPU PDF rendering with NO open DB write lock.
            plan, _, llm_meta = await self.resume_service.plan_resume(
                None,
                doc=doc,
                job_title=job_title,
                job_company=job_company,
                job_location=job_location,
                job_workplace=job_workplace,
                job_source=job_source,
                job_description=job_description,
                remaining_budget_override=remaining_budget,
            )
            rendered = self.resume_service.render_and_verify_pdf(plan=plan, doc=doc)
            new_pdf_path = Path(rendered["pdf_path"])

            # 3. Short write publish session — recheck versions before committing atomically.
            try:
                with session_factory() as write_db:
                    current_job = write_db.scalar(select(JobPosting).where(JobPosting.id == job_id))
                    if current_job is None:
                        raise JobNotFoundError("Vaga removida durante o preparo.")
                    _, latest_profile_rev = self.profile_service.get_document(
                        write_db, user_id=user_id
                    )
                    if latest_profile_rev != profile_rev or current_job.description_hash != job_desc_hash:
                        raise PrepareConflictError(
                            "Perfil mestre ou vaga foram atualizados durante a geração do PDF. Tente preparar novamente."
                        )

                    self.resume_service.llm.record_usage(
                        write_db,
                        operation="resume_plan",
                        provider=str(llm_meta.get("provider") or "offline"),
                        model=str(llm_meta.get("model") or ""),
                        prompt_tokens=int(llm_meta.get("prompt_tokens") or 0),
                        completion_tokens=int(llm_meta.get("completion_tokens") or 0),
                        estimated_cost_usd=float(llm_meta.get("estimated_cost_usd") or 0.0),
                    )
                    rv = self.resume_service.persist_rendered_resume(
                        write_db,
                        job=current_job,
                        profile_revision=profile_rev,
                        job_description_hash=job_desc_hash,
                        rendered=rendered,
                        plan=plan,
                        llm_meta=llm_meta,
                        user_id=user_id,
                        commit=False,
                    )

                    app_row: Application | None = None
                    if existing_app_id is not None:
                        app_row = write_db.scalar(
                            select(Application).where(
                                Application.id == existing_app_id,
                                Application.user_id == user_id,
                            )
                        )
                    if app_row is not None and app_row.status in RESUMABLE_STATUSES:
                        app_row.resume_version_id = rv.id
                        app_row.last_error = None
                        if app_row.status not in {
                            ApplicationStatus.REVIEW.value,
                            ApplicationStatus.NEEDS_USER.value,
                        }:
                            app_row.status = ApplicationStatus.READY.value
                    else:
                        app_row = Application(
                            job_id=current_job.id,
                            user_id=user_id,
                            status=ApplicationStatus.READY.value,
                            resume_version_id=rv.id,
                        )
                        write_db.add(app_row)
                    write_db.flush()

                    write_db.add(
                        AuditEvent(
                            actor=user_id,
                            event_type="application.prepared",
                            entity_type="application",
                            entity_id=app_row.id,
                            details={
                                "job_id": current_job.id,
                                "resume_id": rv.id,
                                "profile_revision": profile_rev,
                                "mode": redact_secrets(
                                    str(llm_meta.get("mode") or "offline_deterministic"),
                                    self.settings.secret_values(),
                                ),
                            },
                        )
                    )
                    write_db.commit()
                    write_db.refresh(app_row)
                    payload = self._serialize_application(
                        app_row,
                        current_job,
                        rv,
                        current_profile_revision=profile_rev,
                    )
                    return {
                        "application": payload,
                        "reused_pdf": False,
                        "already_done": False,
                    }
            except Exception:
                if new_pdf_path.exists():
                    from contextlib import suppress

                    with suppress(OSError):
                        new_pdf_path.unlink()
                raise

    def list_applications(
        self,
        db: Session,
        *,
        user_id: str = "local-applicant",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List non-closed applications ordered most recent first."""
        _, current_rev = self.profile_service.get_document(db, user_id=user_id)
        apps = list(
            db.scalars(
                select(Application)
                .where(
                    Application.user_id == user_id,
                    Application.status.in_(tuple(ACTIVE_PANEL_STATUSES)),
                )
                .order_by(Application.updated_at.desc(), Application.created_at.desc(), Application.id.desc())
                .limit(limit)
            ).all()
        )
        job_ids = {a.job_id for a in apps}
        rv_ids = {a.resume_version_id for a in apps if a.resume_version_id}
        jobs = (
            {j.id: j for j in db.scalars(select(JobPosting).where(JobPosting.id.in_(job_ids))).all()}
            if job_ids
            else {}
        )
        resumes = (
            {r.id: r for r in db.scalars(select(ResumeVersion).where(ResumeVersion.id.in_(rv_ids))).all()}
            if rv_ids
            else {}
        )
        return [
            self._serialize_application(
                app,
                jobs.get(app.job_id),
                resumes.get(app.resume_version_id) if app.resume_version_id else None,
                current_profile_revision=current_rev,
            )
            for app in apps
        ]

    def get_application(
        self,
        db: Session,
        *,
        application_id: str,
        user_id: str = "local-applicant",
    ) -> dict[str, Any]:
        """Return serialized application state for an owned application."""
        app = db.scalar(
            select(Application).where(
                Application.id == application_id,
                Application.user_id == user_id,
            )
        )
        if app is None:
            raise ApplicationNotFoundError("Candidatura não encontrada.")
        _, current_rev = self.profile_service.get_document(db, user_id=user_id)
        job = db.scalar(select(JobPosting).where(JobPosting.id == app.job_id))
        rv = (
            db.scalar(select(ResumeVersion).where(ResumeVersion.id == app.resume_version_id))
            if app.resume_version_id
            else None
        )
        return self._serialize_application(app, job, rv, current_profile_revision=current_rev)

    def complete_application(
        self,
        db: Session,
        *,
        application_id: str,
        user_id: str = "local-applicant",
    ) -> dict[str, Any]:
        """Idempotently record user-declared completion with no receipt or remote verification."""
        app = db.scalar(
            select(Application).where(
                Application.id == application_id,
                Application.user_id == user_id,
            )
        )
        if app is None:
            raise ApplicationNotFoundError("Candidatura não encontrada.")
        if app.status == ApplicationStatus.CLOSED.value:
            raise ValueError("Candidatura histórica encerrada não pode ser concluída.")

        if app.status != ApplicationStatus.DONE.value:
            app.status = ApplicationStatus.DONE.value
            app.last_error = None
            db.add(
                AuditEvent(
                    actor=user_id,
                    event_type="application.completed",
                    entity_type="application",
                    entity_id=app.id,
                    details={"job_id": app.job_id, "declared_by_user": True},
                )
            )
            db.commit()
            db.refresh(app)

        return self.get_application(db, application_id=app.id, user_id=user_id)
