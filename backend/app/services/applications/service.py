"""Application preparation, artifact reuse, listing, and local completion service."""

from __future__ import annotations

import asyncio
import json
import threading
import weakref
from pathlib import Path
from typing import Any

from playwright.async_api import Error as PlaywrightError
from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.browser_automation.driver import (
    StepChangedError,
    StepSnapshot,
    inspect_step,
    normalize_label_text,
    resolve_option_for_answer,
    run_step_fill_pass,
)
from backend.app.browser_automation.session import (
    BoundPageInvalidError,
    BrowserSession,
    BrowserUnavailableError,
)
from backend.app.core.config import Settings
from backend.app.core.state_machine import (
    ACTIVE_PANEL_STATUSES,
    RESUMABLE_STATUSES,
    ApplicationStatus,
)
from backend.app.db.session import get_session_factory
from backend.app.models.tables import Application, AuditEvent, JobPosting, ResumeVersion
from backend.app.schemas.contracts import AnswerQuestionItem
from backend.app.schemas.profile import StandardAnswerEntry, new_item_id
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


def _get_lock_for_key(database_url: str, user_id: str, scope_id: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    with _LOCKS_GUARD:
        per_loop = _LOOP_LOCKS.get(loop)
        if per_loop is None:
            per_loop = {}
            _LOOP_LOCKS[loop] = per_loop
        key = (database_url, user_id, scope_id)
        lock = per_loop.get(key)
        if lock is None:
            lock = asyncio.Lock()
            per_loop[key] = lock
        return lock


def _get_prepare_lock(database_url: str, user_id: str, job_id: str) -> asyncio.Lock:
    return _get_lock_for_key(database_url, user_id, f"prep:{job_id}")


def _get_app_op_lock(database_url: str, user_id: str, application_id: str) -> asyncio.Lock:
    return _get_lock_for_key(database_url, user_id, f"op:{application_id}")


class JobNotFoundError(LookupError):
    """Raised when the requested job posting does not exist."""


class ApplicationNotFoundError(LookupError):
    """Raised when the requested application does not exist or is not owned by the user."""


class PrepareConflictError(RuntimeError):
    """Raised when profile revision or job description changes mid-prepare before publish."""


class OperationInProgressError(RuntimeError):
    """Raised when another browser or question mutation is already running for this application."""


class StepConflictError(RuntimeError):
    """Raised when the portal step or snapshot changed before mutation could complete."""

    def __init__(self, code: str, message: str, *, written_count: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.written_count = written_count


class ResumeArtifactMissingError(ValueError):
    """Raised when a resumable historical application has no readable PDF artifact."""


class ApplicationStateError(ValueError):
    """Raised when an application state cannot enter interactive browser workflow."""


class InvalidStepAnswerError(ValueError):
    """Raised when an answer submission targets non-answerable controls or invalid options."""


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
        bound_to_browser: bool | None = None,
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
            "bound_to_browser": bool(bound_to_browser),
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
        browser_session: BrowserSession | None = None,
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
                bound_to_browser=(
                    browser_session.get_application_page(app.id) is not None
                    if browser_session is not None
                    else False
                ),
            )
            for app in apps
        ]

    def get_application(
        self,
        db: Session,
        *,
        application_id: str,
        user_id: str = "local-applicant",
        browser_session: BrowserSession | None = None,
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
        return self._serialize_application(
            app,
            job,
            rv,
            current_profile_revision=current_rev,
            bound_to_browser=(
                browser_session.get_application_page(app.id) is not None
                if browser_session is not None
                else False
            ),
        )

    def complete_application(
        self,
        db: Session,
        *,
        application_id: str,
        user_id: str = "local-applicant",
        browser_session: BrowserSession | None = None,
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

        return self.get_application(
            db, application_id=app.id, user_id=user_id, browser_session=browser_session
        )

    def _redact_step_dict(self, payload: dict[str, Any]) -> dict[str, Any]:
        secrets_list = self.settings.secret_values()
        serialized = json.dumps(payload, ensure_ascii=False)
        redacted = redact_secrets(serialized, secrets_list)
        return dict(json.loads(redacted))

    def _build_initial_step_summary(self, snapshot: StepSnapshot) -> dict[str, Any]:
        notices: list[str] = []
        if snapshot.profile_surface_warning:
            notices.append(snapshot.profile_surface_warning)
        blockers: list[dict[str, Any]] = []
        if snapshot.open_form_required:
            blockers.append(
                {
                    "code": "open_form_required",
                    "message": "Abra o formulário da vaga no navegador e clique em Preencher etapa atual.",
                    "can_answer": False,
                }
            )
        elif snapshot.login_required or snapshot.has_otp:
            blockers.append(
                {
                    "code": "otp_required" if snapshot.has_otp else "login_required",
                    "message": (
                        "Autenticação ou código OTP exigido no navegador. "
                        "Conclua no Chrome e clique em Preencher etapa atual."
                    ),
                    "can_answer": False,
                }
            )
        elif snapshot.has_captcha:
            blockers.append(
                {
                    "code": "captcha",
                    "message": "CAPTCHA detectado nesta etapa. Resolva no Chrome e clique em Preencher etapa atual.",
                    "can_answer": False,
                }
            )
        return self._redact_step_dict(
            {
                "step_id": snapshot.step_id,
                "portal": snapshot.portal,
                "page_url": snapshot.page_url,
                "page_origin": snapshot.page_origin,
                "heading": snapshot.heading,
                "open_form_required": snapshot.open_form_required,
                "final_submit_detected": snapshot.final_submit_detected,
                "filled": [],
                "skipped": [],
                "uploaded_files": [],
                "blockers": blockers,
                "notices": notices,
                "pending_refill": False,
            }
        )

    async def open_application(
        self,
        *,
        application_id: str,
        browser_session: BrowserSession,
        user_id: str = "local-applicant",
    ) -> dict[str, Any]:
        """Open (or focus without re-navigating) a persistent Chrome tab for this application."""
        lock = _get_app_op_lock(self.settings.database_url, user_id, application_id)
        if lock.locked():
            raise OperationInProgressError("Outra operação desta candidatura está em andamento.")

        session_factory = get_session_factory(self.settings)
        async with lock:
            with session_factory() as read_db:
                app = read_db.scalar(
                    select(Application).where(
                        Application.id == application_id,
                        Application.user_id == user_id,
                    )
                )
                if app is None:
                    raise ApplicationNotFoundError("Candidatura não encontrada.")
                if app.status in {ApplicationStatus.CLOSED.value, ApplicationStatus.DONE.value}:
                    raise ApplicationStateError(
                        "Candidatura já concluída ou encerrada não pode abrir guia ativa."
                    )
                job = read_db.scalar(select(JobPosting).where(JobPosting.id == app.job_id))
                if job is None or not (job.apply_url or job.job_url):
                    raise ApplicationStateError("URL da vaga indisponível.")
                target_url = job.apply_url or job.job_url
                job_title = job.title
                job_company = job.company

            try:
                page = await browser_session.open_application_page(application_id, target_url)
                nav_gen = browser_session.get_navigation_generation(page)
                snapshot = await inspect_step(
                    page,
                    navigation_gen=nav_gen,
                    expected_job_title=job_title,
                    expected_company=job_company,
                )
                browser_session.store_step_snapshot(application_id, snapshot)
                step_summary = self._build_initial_step_summary(snapshot)
            except BrowserUnavailableError:
                raise
            except PlaywrightError as exc:
                raise BrowserUnavailableError(
                    "Falha de comunicação com a aba do navegador. Verifique o Chrome e tente novamente."
                ) from exc

            with session_factory() as write_db:
                app_w = write_db.scalar(
                    select(Application).where(
                        Application.id == application_id,
                        Application.user_id == user_id,
                    )
                )
                if app_w is None:
                    raise ApplicationNotFoundError("Candidatura removida durante abertura.")
                app_w.last_step_json = step_summary
                app_w.last_error = None
                write_db.add(
                    AuditEvent(
                        actor=user_id,
                        event_type="application.opened",
                        entity_type="application",
                        entity_id=app_w.id,
                        details={"portal": snapshot.portal, "page_origin": snapshot.page_origin},
                    )
                )
                write_db.commit()
                app_payload = self.get_application(
                    write_db,
                    application_id=application_id,
                    user_id=user_id,
                    browser_session=browser_session,
                )

            return {
                "opened": True,
                "bound_to_browser": True,
                "portal": snapshot.portal,
                "page_url": snapshot.page_url,
                "step_id": snapshot.step_id,
                "open_form_required": snapshot.open_form_required,
                "application": app_payload,
            }

    async def list_candidate_pages(
        self,
        *,
        application_id: str,
        browser_session: BrowserSession,
        user_id: str = "local-applicant",
    ) -> list[dict[str, Any]]:
        """List safe attachable Chrome tabs with opaque server-generated IDs and vacancy context."""
        session_factory = get_session_factory(self.settings)
        with session_factory() as db:
            app = db.scalar(
                select(Application).where(
                    Application.id == application_id,
                    Application.user_id == user_id,
                )
            )
            if app is None:
                raise ApplicationNotFoundError("Candidatura não encontrada.")
            job = db.scalar(select(JobPosting).where(JobPosting.id == app.job_id))
            expected_title = job.title if job else None
            expected_company = job.company if job else None

        return await browser_session.list_open_pages(
            application_id=application_id,
            expected_title=expected_title,
            expected_company=expected_company,
        )

    async def attach_page(
        self,
        *,
        application_id: str,
        tab_id: str,
        browser_session: BrowserSession,
        user_id: str = "local-applicant",
    ) -> dict[str, Any]:
        """Explicitly attach a candidate Chrome tab identified only by opaque tab_id."""
        lock = _get_app_op_lock(self.settings.database_url, user_id, application_id)
        if lock.locked():
            raise OperationInProgressError("Outra operação desta candidatura está em andamento.")

        session_factory = get_session_factory(self.settings)
        async with lock:
            with session_factory() as read_db:
                app = read_db.scalar(
                    select(Application).where(
                        Application.id == application_id,
                        Application.user_id == user_id,
                    )
                )
                if app is None:
                    raise ApplicationNotFoundError("Candidatura não encontrada.")
                if app.status in {ApplicationStatus.CLOSED.value, ApplicationStatus.DONE.value}:
                    raise ApplicationStateError(
                        "Candidatura encerrada ou concluída não aceita nova vinculação."
                    )
                job = read_db.scalar(select(JobPosting).where(JobPosting.id == app.job_id))
                job_title = job.title if job else ""
                job_company = job.company if job else ""
                job_url = (job.apply_url or job.job_url) if job else None

            page = await browser_session.attach_application_page(application_id, tab_id=tab_id)
            snapshot = await inspect_step(
                page,
                navigation_gen=browser_session.get_navigation_generation(page),
                expected_job_title=job_title,
                expected_company=job_company,
            )

            if snapshot.vacancy_mismatch:
                browser_session.detach_application_page(application_id)
                raise BoundPageInvalidError(
                    "vacancy_mismatch",
                    f"A aba escolhida exibe outra vaga ({snapshot.vacancy_context_title}). Escolha a aba correta ou use Reabrir no navegador.",
                )

            has_form_or_gate = bool(
                snapshot.container_selector
                or snapshot.fields
                or snapshot.open_form_required
                or snapshot.login_required
                or snapshot.has_captcha
                or snapshot.cross_origin_iframes
                or snapshot.multiple_containers
            )
            matches_title_or_site = False
            if job_url:
                from backend.app.browser_automation.adapters import is_same_site_origin
                from backend.app.core.ssrf import extract_origin

                if is_same_site_origin(extract_origin(job_url), snapshot.page_origin):
                    matches_title_or_site = True
            if not has_form_or_gate and not matches_title_or_site:
                # Allow clear keyword overlap in heading/page title for landing tabs
                hay = normalize_label_text(f"{snapshot.heading} {snapshot.vacancy_context_title}")
                terms = [
                    tok
                    for tok in normalize_label_text(f"{job_title} {job_company}").split()
                    if len(tok) >= 3
                ]
                if not any(t in hay for t in terms):
                    browser_session.detach_application_page(application_id)
                    raise BoundPageInvalidError(
                        "context_mismatch",
                        "A aba selecionada não possui formulário nem contexto reconhecível desta vaga.",
                    )

            browser_session.store_step_snapshot(application_id, snapshot)
            step_summary = self._build_initial_step_summary(snapshot)

            with session_factory() as write_db:
                app_w = write_db.scalar(
                    select(Application).where(
                        Application.id == application_id,
                        Application.user_id == user_id,
                    )
                )
                if app_w is None:
                    raise ApplicationNotFoundError("Candidatura não encontrada.")
                app_w.last_step_json = step_summary
                app_w.last_error = None
                write_db.add(
                    AuditEvent(
                        actor=user_id,
                        event_type="application.page_attached",
                        entity_type="application",
                        entity_id=app_w.id,
                        details={"portal": snapshot.portal, "page_origin": snapshot.page_origin},
                    )
                )
                write_db.commit()
                app_payload = self.get_application(
                    write_db,
                    application_id=application_id,
                    user_id=user_id,
                    browser_session=browser_session,
                )

            return {
                "attached": True,
                "portal": snapshot.portal,
                "page_url": snapshot.page_url,
                "page_origin": snapshot.page_origin,
                "step_id": snapshot.step_id,
                "application": app_payload,
            }

    async def fill_current_page(
        self,
        *,
        application_id: str,
        browser_session: BrowserSession,
        user_id: str = "local-applicant",
    ) -> dict[str, Any]:
        """Inspect and perform bounded fill for ONLY the current visible portal step."""
        lock = _get_app_op_lock(self.settings.database_url, user_id, application_id)
        if lock.locked():
            raise OperationInProgressError("Outra operação desta candidatura está em andamento.")

        session_factory = get_session_factory(self.settings)
        async with lock:
            # 1. Short read session — check app, artifact, answers, profile, budget BEFORE any CDP/LLM work.
            with session_factory() as read_db:
                app = read_db.scalar(
                    select(Application).where(
                        Application.id == application_id,
                        Application.user_id == user_id,
                    )
                )
                if app is None:
                    raise ApplicationNotFoundError("Candidatura não encontrada.")
                if app.status in {ApplicationStatus.CLOSED.value, ApplicationStatus.DONE.value}:
                    raise ApplicationStateError(
                        "Candidatura já concluída ou encerrada; não é possível preencher etapas."
                    )

                job = read_db.scalar(select(JobPosting).where(JobPosting.id == app.job_id))
                rv = (
                    read_db.scalar(
                        select(ResumeVersion).where(ResumeVersion.id == app.resume_version_id)
                    )
                    if app.resume_version_id
                    else None
                )
                if rv is None:
                    raise ResumeArtifactMissingError(
                        "Currículo PDF ausente ou expirado. Clique em Preparar & Aplicar para gerar um novo PDF antes de preencher."
                    )
                try:
                    resume_pdf_path = resolve_safe_resume_path(self.settings, rv, user_id=user_id)
                    reader = PdfReader(str(resume_pdf_path))
                    if not reader.pages:
                        raise ResumeArtifactMissingError("Currículo PDF vazio; gere novamente.")
                except ResumeArtifactMissingError:
                    raise
                except Exception as exc:
                    raise ResumeArtifactMissingError(
                        "Currículo PDF ausente ou ilegível. Regere o PDF antes de preencher."
                    ) from exc

                doc, _ = self.profile_service.get_document(read_db, user_id=user_id)
                answers_json = dict(app.answers_json or {})
                remaining_budget = self.resume_service.llm.remaining_budget_usd(read_db)
                job_url = (job.apply_url or job.job_url) if job else None
                job_title = job.title if job else ""
                job_company = job.company if job else ""
                job_description = job.description if job else ""

            # 2. Browser + optional model I/O with NO open database write transaction.
            await browser_session.ensure_connected()
            try:
                page = browser_session.validate_bound_page(application_id, job_url=job_url)
            except BoundPageInvalidError as exc:
                recovery_blocker = {
                    "code": "unknown_origin"
                    if exc.code in {"origin_changed", "unsafe_origin"}
                    else exc.code,
                    "message": str(exc),
                    "can_answer": False,
                }
                recovery_step = self._redact_step_dict(
                    {
                        "step_id": None,
                        "portal": "unknown",
                        "page_url": None,
                        "page_origin": None,
                        "heading": "",
                        "filled": [],
                        "skipped": [],
                        "uploaded_files": [],
                        "blockers": [recovery_blocker],
                        "notices": [],
                        "final_submit_detected": False,
                        "pending_refill": False,
                    }
                )
                with session_factory() as err_db:
                    app_e = err_db.scalar(
                        select(Application).where(
                            Application.id == application_id,
                            Application.user_id == user_id,
                        )
                    )
                    if app_e is not None:
                        app_e.status = ApplicationStatus.NEEDS_USER.value
                        app_e.last_step_json = recovery_step
                        app_e.last_error = str(exc)
                        err_db.commit()
                        app_payload = self.get_application(
                            err_db,
                            application_id=application_id,
                            user_id=user_id,
                            browser_session=browser_session,
                        )
                    else:
                        app_payload = {}
                return {
                    "status": ApplicationStatus.NEEDS_USER.value,
                    "step_id": None,
                    "filled": [],
                    "skipped": [],
                    "uploaded_files": [],
                    "blockers": [recovery_blocker],
                    "notices": [],
                    "final_submit_detected": False,
                    "application": app_payload,
                }

            try:
                run_res = await run_step_fill_pass(
                    page,
                    browser_session,
                    application_id,
                    doc=doc,
                    answers_json=answers_json,
                    resume_pdf_path=resume_pdf_path,
                    job_url=job_url,
                    job_title=job_title,
                    job_company=job_company,
                    job_description=job_description,
                    llm_gateway=self.resume_service.llm,
                    db=None,
                    remaining_budget_usd=remaining_budget,
                )
            except StepChangedError as exc:
                raise StepConflictError("step_changed", str(exc), written_count=exc.written_count) from exc
            except BoundPageInvalidError as exc:
                raise StepConflictError("origin_changed", str(exc), written_count=0) from exc
            except PlaywrightError as exc:
                raise BrowserUnavailableError(
                    "Conexão transitória perdida com a aba do Chrome. Verifique a aba ou use Anexar aba aberta."
                ) from exc

            snapshot = run_res.snapshot
            if snapshot.vacancy_mismatch:
                browser_session.detach_application_page(application_id)

            blocker_dicts = [b.to_public_dict() for b in run_res.blockers]
            next_status = (
                ApplicationStatus.NEEDS_USER.value
                if blocker_dicts
                else ApplicationStatus.REVIEW.value
            )
            step_payload = self._redact_step_dict(
                {
                    "step_id": snapshot.step_id,
                    "portal": snapshot.portal,
                    "page_url": snapshot.page_url,
                    "page_origin": snapshot.page_origin,
                    "heading": snapshot.heading,
                    "open_form_required": snapshot.open_form_required,
                    "final_submit_detected": snapshot.final_submit_detected,
                    "has_resume_upload": snapshot.has_resume_upload,
                    "filled": run_res.filled,
                    "skipped": run_res.skipped,
                    "uploaded_files": run_res.uploaded_files,
                    "blockers": blocker_dicts,
                    "notices": run_res.notices,
                    "pending_refill": False,
                }
            )

            # 3. Persist final report afterward (writes already visible in browser are never rolled back).
            with session_factory() as write_db:
                for usage in run_res.llm_usage_events:
                    self.resume_service.llm.record_usage(
                        write_db,
                        operation=str(usage.get("operation") or "step_field_plan"),
                        provider=str(usage.get("provider") or "offline"),
                        model=str(usage.get("model") or ""),
                        prompt_tokens=int(usage.get("prompt_tokens") or 0),
                        completion_tokens=int(usage.get("completion_tokens") or 0),
                        estimated_cost_usd=float(usage.get("estimated_cost_usd") or 0.0),
                    )
                app_w = write_db.scalar(
                    select(Application).where(
                        Application.id == application_id,
                        Application.user_id == user_id,
                    )
                )
                if app_w is None:
                    raise ApplicationNotFoundError("Candidatura não encontrada após preenchimento.")
                app_w.status = next_status
                app_w.last_step_json = step_payload
                app_w.last_error = blocker_dicts[0]["message"] if blocker_dicts else None
                write_db.add(
                    AuditEvent(
                        actor=user_id,
                        event_type="application.step_filled",
                        entity_type="application",
                        entity_id=app_w.id,
                        details={
                            "status": next_status,
                            "portal": snapshot.portal,
                            "filled_count": len(step_payload["filled"]),
                            "blocker_count": len(step_payload["blockers"]),
                        },
                    )
                )
                write_db.commit()
                app_payload = self.get_application(
                    write_db,
                    application_id=application_id,
                    user_id=user_id,
                    browser_session=browser_session,
                )

            return {
                "status": next_status,
                "step_id": step_payload["step_id"],
                "portal": step_payload["portal"],
                "page_url": step_payload["page_url"],
                "final_submit_detected": step_payload["final_submit_detected"],
                "filled": step_payload["filled"],
                "skipped": step_payload["skipped"],
                "uploaded_files": step_payload["uploaded_files"],
                "blockers": step_payload["blockers"],
                "notices": step_payload["notices"],
                "application": app_payload,
            }

    async def answer_questions(
        self,
        *,
        application_id: str,
        step_id: str,
        answers: list[AnswerQuestionItem],
        profile_revision: int | None,
        browser_session: BrowserSession,
        user_id: str = "local-applicant",
    ) -> dict[str, Any]:
        """Store step-scoped question answers (and opted-in reusable answers atomically via CAS)."""
        lock = _get_app_op_lock(self.settings.database_url, user_id, application_id)
        if lock.locked():
            raise OperationInProgressError("Outra operação desta candidatura está em andamento.")

        session_factory = get_session_factory(self.settings)
        async with lock:
            # 1. Verify current snapshot / bound page still matches step_id when bound in browser
            snapshot = browser_session.get_step_snapshot(application_id, step_id)
            latest_snap = browser_session.get_latest_snapshot(application_id)
            if latest_snap is not None and latest_snap.step_id != step_id:
                raise StepConflictError(
                    "step_changed",
                    "Esta etapa não é mais a mais recente; clique em Preencher etapa atual para atualizar.",
                )

            with session_factory() as read_db:
                app = read_db.scalar(
                    select(Application).where(
                        Application.id == application_id,
                        Application.user_id == user_id,
                    )
                )
                if app is None:
                    raise ApplicationNotFoundError("Candidatura não encontrada.")
                if app.status in {ApplicationStatus.CLOSED.value, ApplicationStatus.DONE.value}:
                    raise ApplicationStateError("Candidatura encerrada ou concluída não aceita respostas.")
                job = read_db.scalar(select(JobPosting).where(JobPosting.id == app.job_id))
                job_url = (job.apply_url or job.job_url) if job else None
                job_title = job.title if job else ""
                job_company = job.company if job else ""
                persisted_step = dict(app.last_step_json or {})

            if not persisted_step or persisted_step.get("step_id") != step_id:
                raise StepConflictError(
                    "step_changed",
                    "Identificador de etapa inválido ou expirado; reinspicione a etapa no navegador.",
                )

            # Revalidate live page binding & structural fingerprint if bound page exists
            if browser_session.get_application_page(application_id) is not None:
                try:
                    page = browser_session.validate_bound_page(application_id, job_url=job_url)
                except BoundPageInvalidError as exc:
                    raise StepConflictError("step_changed", str(exc)) from exc
                if snapshot is not None:
                    if browser_session.get_navigation_generation(page) != snapshot.navigation_gen:
                        raise StepConflictError(
                            "step_changed",
                            "A página navegou desde a inspeção; clique em Preencher etapa atual.",
                        )
                    fresh = await inspect_step(
                        page,
                        navigation_gen=snapshot.navigation_gen,
                        expected_job_title=job_title,
                        expected_company=job_company,
                    )
                    if fresh.structural_fingerprint != snapshot.structural_fingerprint:
                        raise StepConflictError(
                            "step_changed",
                            "O formulário mudou no navegador; clique em Preencher etapa atual.",
                        )

            answerable_by_id: dict[str, dict[str, Any]] = {}
            for blk in list(persisted_step.get("blockers") or []):
                if blk.get("can_answer") and blk.get("field_id"):
                    answerable_by_id[str(blk["field_id"])] = dict(blk)

            field_desc_by_id = (
                {f.field_id: f for f in snapshot.fields} if snapshot is not None else {}
            )

            resolved_updates: dict[str, str] = {}
            profile_entries: list[StandardAnswerEntry] = []
            answered_field_ids: set[str] = set()

            for item in answers:
                blk = answerable_by_id.get(item.field_id)
                if blk is None:
                    raise InvalidStepAnswerError(
                        f"Campo '{item.field_id}' não aceita resposta nesta etapa ou exige ação manual no Chrome."
                    )
                sig = str(blk.get("question_signature") or "")
                label = str(blk.get("label") or item.field_id)
                field_type = str(blk.get("field_type") or "text")
                if not sig:
                    raise InvalidStepAnswerError(f"Assinatura de pergunta ausente para {label}.")

                canonical_value = item.value.strip()
                desc = field_desc_by_id.get(item.field_id)
                raw_options = list(blk.get("options") or [])
                if raw_options or (desc is not None and desc.options):
                    from backend.app.browser_automation.driver import FieldOption

                    opts_tuple = (
                        desc.options
                        if desc is not None and desc.options
                        else tuple(
                            FieldOption(
                                option_id=str(o.get("option_id") or ""),
                                label=str(o.get("label") or ""),
                            )
                            for o in raw_options
                        )
                    )
                    matched_opt = next(
                        (
                            o
                            for o in opts_tuple
                            if o.option_id == canonical_value
                            or normalize_label_text(o.label) == normalize_label_text(canonical_value)
                        ),
                        None,
                    )
                    if matched_opt is None:
                        matched_opt = resolve_option_for_answer(canonical_value, opts_tuple)
                    if matched_opt is None:
                        raise InvalidStepAnswerError(
                            f"Opção '{item.value}' não corresponde às opções disponíveis para '{label}'."
                        )
                    canonical_value = matched_opt.label

                resolved_updates[sig] = canonical_value
                answered_field_ids.add(item.field_id)

                if item.save_to_profile:
                    kind_literal = (
                        "option"
                        if (raw_options or field_type in {"option", "select", "radio"})
                        else ("boolean" if field_type == "boolean" else ("number" if field_type == "number" else "text"))
                    )
                    profile_entries.append(
                        StandardAnswerEntry(
                            id=new_item_id("std"),
                            question_signature=sig,
                            label=label[:400],
                            answer=canonical_value[:4000],
                            kind=kind_literal,  # type: ignore[arg-type]
                            source="application_copilot",
                        )
                    )

            with session_factory() as write_db:
                app_w = write_db.scalar(
                    select(Application).where(
                        Application.id == application_id,
                        Application.user_id == user_id,
                    )
                )
                if app_w is None:
                    raise ApplicationNotFoundError("Candidatura não encontrada.")
                merged_answers = dict(app_w.answers_json or {})
                merged_answers.update(resolved_updates)
                app_w.answers_json = merged_answers

                remaining_blockers = [
                    blk
                    for blk in list(persisted_step.get("blockers") or [])
                    if str(blk.get("field_id") or "") not in answered_field_ids
                ]
                updated_step = dict(persisted_step)
                updated_step["blockers"] = remaining_blockers
                updated_step["pending_refill"] = True
                app_w.last_step_json = self._redact_step_dict(updated_step)
                app_w.last_error = remaining_blockers[0]["message"] if remaining_blockers else None

                new_profile_rev: int | None = None
                if profile_entries:
                    prof_res = self.profile_service.upsert_standard_answers(
                        write_db,
                        entries=profile_entries,
                        expected_revision=profile_revision,
                        user_id=user_id,
                        commit=False,
                    )
                    new_profile_rev = int(prof_res["revision"])

                write_db.add(
                    AuditEvent(
                        actor=user_id,
                        event_type="application.questions_answered",
                        entity_type="application",
                        entity_id=app_w.id,
                        details={
                            "saved_count": len(resolved_updates),
                            "saved_to_profile_count": len(profile_entries),
                        },
                    )
                )
                write_db.commit()
                app_payload = self.get_application(
                    write_db,
                    application_id=application_id,
                    user_id=user_id,
                    browser_session=browser_session,
                )

            return {
                "saved_count": len(resolved_updates),
                "saved_to_profile_count": len(profile_entries),
                "step_id": step_id,
                "profile_revision": new_profile_rev,
                "application": app_payload,
            }
