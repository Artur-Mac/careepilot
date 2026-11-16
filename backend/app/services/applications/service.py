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
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.app.browser_automation.driver import (
    StepChangedError,
    StepSnapshot,
    classify_field_category,
    inspect_step,
    normalize_label_text,
    resolve_option_for_answer,
    run_step_fill_pass,
)
from backend.app.browser_automation.preflight import (
    PORTAL_REGISTRY,
    PortalScope,
    _match_portal_for_url,
    probe_session,
)
from backend.app.browser_automation.session import (
    BoundPageInvalidError,
    BrowserSession,
    BrowserUnavailableError,
)
from backend.app.core.config import Settings, is_local_llm_url
from backend.app.core.llm import get_engine_telemetry
from backend.app.core.state_machine import (
    ACTIVE_PANEL_STATUSES,
    RESUMABLE_STATUSES,
    ApplicationStatus,
)
from backend.app.core.time import utc_rfc3339
from backend.app.db.session import get_session_factory
from backend.app.models.tables import (
    Application,
    ArsenalRespostaRow,
    AuditEvent,
    JobPosting,
    PendingManualActionRow,
    PortalSessionStatusRow,
    ResumeVersion,
)
from backend.app.schemas.contracts import (
    AnswerQuestionItem,
    ApprovePendingActionRequest,
    SkipPendingActionRequest,
)
from backend.app.schemas.profile import Locale, StandardAnswerEntry, new_item_id
from backend.app.security.redaction import redact_secrets
from backend.app.security.vault import SensitivityLevel, classify_field_sensitivity
from backend.app.services.applications.arsenal import (
    ArsenalValidationError,
    build_confirmed_answer_arsenal_draft,
    match_arsenal_for_step,
)
from backend.app.services.applications.rules import build_verified_slot_values, match_field_rule
from backend.app.services.applications.salary import (
    ApproveSalaryRequest,
    PendingActionConflictError,
    PendingActionValidationError,
    SalaryQueue,
    _field_shape,
    salary_context,
    serialize_pending_action_row,
)
from backend.app.services.applications.validator import (
    ControlConstraints,
    extract_control_constraints,
    validate_candidate_value,
)
from backend.app.services.profile.routing import JobContext
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

    def _can_use_llm_for_profile(self) -> bool:
        provider = self.settings.llm_provider.strip().lower()
        if provider not in {"qwen_local", "openai_compat"}:
            return False
        return is_local_llm_url(self.settings.llm_base_url) or bool(
            self.settings.llm_allow_remote_profile
        )

    def _is_reusable_resume(
        self,
        db: Session,
        *,
        resume_id: str | None,
        user_id: str,
        profile_rev: int,
        job_desc_hash: str,
        locale: Locale,
    ) -> ResumeVersion | None:
        if not resume_id:
            return None
        rv = db.scalar(select(ResumeVersion).where(ResumeVersion.id == resume_id))
        if rv is None or rv.user_id != user_id:
            return None
        if (
            rv.profile_revision != profile_rev
            or rv.job_description_hash != job_desc_hash
            or (rv.diff_summary or {}).get("language") != locale
        ):
            return None
        try:
            path = resolve_safe_resume_path(self.settings, rv, user_id=user_id)
            reader = PdfReader(str(path))
            if len(reader.pages) != 1:
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
        locale: Locale | None = None,
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

                language = locale or self.resume_service.resume_language(job.title, job.description)
                if existing_resumable is not None:
                    reusable_rv = self._is_reusable_resume(
                        read_db,
                        resume_id=existing_resumable.resume_version_id,
                        user_id=user_id,
                        profile_rev=profile_rev,
                        job_desc_hash=job.description_hash,
                        locale=language,
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

                job_title = job.title
                job_company = job.company
                job_location = job.location
                job_workplace = job.workplace_type
                seniority, contract = job.seniority or "", job.employment_type or ""
                job_source = job.source
                job_description = job.description
                job_desc_hash = job.description_hash
                existing_app_id = existing_resumable.id if existing_resumable else None

            routed_doc = self.resume_service.route_resume(
                doc, job_title, job_description,
                language,
                seniority, contract,
            )
            ctx = JobContext.from_job(
                job_title,
                job_description,
                language,
                [skill.name for skill in doc.skills],
                seniority,
                contract,
            )
            summary_rev = "default"
            routed_summaries = getattr(routed_doc, "summaries", None)
            if routed_summaries:
                s0 = routed_summaries[0]
                summary_rev = f"{s0.id}:{getattr(s0, 'revision', 1)}"
            artifact_key = self.resume_service.compute_resume_artifact_key(
                user_id=user_id,
                profile_revision=profile_rev,
                job_id=job_id,
                job_description_hash=job_desc_hash,
                seniority=ctx.seniority,
                contract=ctx.contract,
                archetype=ctx.archetype,
                locale=language,
                summary_revision=summary_rev,
            )

            # 2. Tailor and render from reviewed profile data outside the write transaction.
            plan, _, llm_meta = await self.resume_service.plan_resume(
                None,
                doc=routed_doc,
                job_title=job_title,
                job_company=job_company,
                job_location=job_location,
                job_workplace=job_workplace,
                job_source=job_source,
                job_description=job_description,
                locale=language,
                deterministic=True,
            )
            rendered = self.resume_service.render_and_verify_pdf(
                plan=plan,
                doc=routed_doc,
                job_title=job_title,
                job_company=job_company,
                job_description=job_description,
            )
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
                        artifact_key=artifact_key,
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

    async def _try_auto_attach_matching_tab(
        self,
        *,
        application_id: str,
        job_title: str,
        job_company: str,
        browser_session: BrowserSession,
        require_form_fields: bool = False,
        exclude_page: Any | None = None,
    ) -> tuple[Any, StepSnapshot] | None:
        """Attempt to auto-attach an already-open Chrome tab that matches the vacancy and has an active form."""
        try:
            open_tabs = await browser_session.list_open_pages(
                application_id=application_id,
                expected_title=job_title or None,
                expected_company=job_company or None,
            )
        except Exception:
            return None

        best_fallback: tuple[ str, Any, StepSnapshot ] | None = None
        for tab in open_tabs:
            if not tab.get("matches_vacancy"):
                continue
            bound_aid = tab.get("bound_application_id")
            if bound_aid and bound_aid != application_id:
                continue
            tab_id = str(tab.get("tab_id") or "")
            rec = browser_session._tab_registry.get(tab_id)
            if rec is None:
                continue
            cand_page, _ = rec
            if cand_page.is_closed() or (exclude_page is not None and cand_page is exclude_page):
                continue
            try:
                cand_snap = await inspect_step(
                    cand_page,
                    navigation_gen=browser_session.get_navigation_generation(cand_page),
                    expected_job_title=job_title,
                    expected_company=job_company,
                )
            except Exception:
                continue
            if cand_snap.vacancy_mismatch:
                continue
            if cand_snap.fields:
                attached = await browser_session.attach_application_page(
                    application_id, tab_id=tab_id
                )
                return attached, cand_snap
            if not require_form_fields and best_fallback is None:
                best_fallback = (tab_id, cand_page, cand_snap)

        if best_fallback is not None:
            tab_id, _, cand_snap = best_fallback
            try:
                attached = await browser_session.attach_application_page(
                    application_id, tab_id=tab_id
                )
                return attached, cand_snap
            except Exception:
                return None
        return None

    async def _try_follow_entry_apply_cta(
        self,
        *,
        application_id: str,
        page: Any,
        browser_session: BrowserSession,
    ) -> bool:
        """When a job landing page has no form yet, follow a visible 'Candidatar-se' / 'Apply' entry link or button."""
        from contextlib import suppress
        from urllib.parse import parse_qs, unquote, urlparse

        from backend.app.core.ssrf import extract_origin

        try:
            cta_info = await page.evaluate(
                """() => {
                    const isVis = (el) => {
                        if (!el || !el.isConnected) return false;
                        const st = window.getComputedStyle(el);
                        if (!st || st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 1 && r.height > 1;
                    };
                    const pat = /(candidatura simplificada|candidatura r[áa]pida|candidatura|candidatar(?:-se|-me)?|apply now|apply for this job|apply on company|easy apply|quick apply|\bapply\b)/i;
                    const forbid = /(enviar candidatura|finalizar|concluir|submit application|minhas candidaturas|ver candidatura|acompanhar candidatura|my applications|saved jobs|^submit$|^enviar$)/i;
                    const primaryNodes = Array.from(
                        document.querySelectorAll('button.jobs-apply-button, .jobs-apply-button button, button[aria-label*="Candidatura"], button[aria-label*="Easy Apply"], button, [role="button"]')
                    ).filter((el) => isVis(el) && !el.closest('header, nav, [role="navigation"]'));
                    const linkNodes = Array.from(
                        document.querySelectorAll('a[href]')
                    ).filter((el) => isVis(el) && !el.closest('header, nav, [role="navigation"]'));
                    for (const el of [...primaryNodes, ...linkNodes]) {
                        const txt = ((el.innerText || '') + ' ' + (el.getAttribute('aria-label') || '')).replace(/\\s+/g, ' ').trim();
                        if (!pat.test(txt) || forbid.test(txt)) continue;
                        const href = el.href || el.getAttribute('href') || '';
                        if (el.tagName === 'A' && href && /^https?:/i.test(href)) {
                            return { kind: 'link', href };
                        }
                        el.click();
                        return { kind: 'clicked', text: txt.slice(0, 80) };
                    }
                    return null;
                }"""
            )
        except Exception:
            return False

        if not cta_info:
            return False

        if cta_info.get("kind") == "link" and cta_info.get("href"):
            raw_href = str(cta_info["href"])
            parsed = urlparse(raw_href)
            if "linkedin.com" in (parsed.hostname or "").lower() and "/safety/go" in parsed.path:
                qs = parse_qs(parsed.query)
                if qs.get("url"):
                    raw_href = unquote(qs["url"][0])
            try:
                final_url = await browser_session.safe_goto(page, raw_href)
                browser_session._app_bound_origins[application_id] = extract_origin(final_url)
                with suppress(Exception):
                    await page.wait_for_load_state("domcontentloaded", timeout=4000)
                return True
            except Exception:
                return False

        if cta_info.get("kind") == "clicked":
            with suppress(Exception):
                await asyncio.sleep(1.2)
            return True
        return False

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
                auto_attached = await self._try_auto_attach_matching_tab(
                    application_id=application_id,
                    job_title=job_title,
                    job_company=job_company,
                    browser_session=browser_session,
                    require_form_fields=True,
                )
                if auto_attached is not None:
                    page, snapshot = auto_attached
                else:
                    page = await browser_session.open_application_page(application_id, target_url)
                    nav_gen = browser_session.get_navigation_generation(page)
                    snapshot = await inspect_step(
                        page,
                        navigation_gen=nav_gen,
                        expected_job_title=job_title,
                        expected_company=job_company,
                    )
                    if snapshot.open_form_required and not snapshot.fields:
                        handoff = await self._try_auto_attach_matching_tab(
                            application_id=application_id,
                            job_title=job_title,
                            job_company=job_company,
                            browser_session=browser_session,
                            require_form_fields=True,
                            exclude_page=page,
                        )
                        if handoff is not None:
                            page, snapshot = handoff
                        elif await self._try_follow_entry_apply_cta(
                            application_id=application_id,
                            page=page,
                            browser_session=browser_session,
                        ):
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
            # Read the application, artifact and known answers before inspecting the live form.
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
                job_url = (job.apply_url or job.job_url) if job else None
                job_title = job.title if job else ""
                job_company = job.company if job else ""
                job_description = job.description if job else ""
                seniority = (job.seniority or "") if job else ""
                contract = (job.employment_type or "") if job else ""

            # Inspect the browser outside the database transaction.
            await browser_session.ensure_connected()
            page: Any | None = None
            bound_err: BoundPageInvalidError | None = None
            try:
                page = browser_session.validate_bound_page(application_id, job_url=job_url)
            except BoundPageInvalidError as exc:
                if exc.code == "lost_closed":
                    auto_attached = await self._try_auto_attach_matching_tab(
                        application_id=application_id,
                        job_title=job_title,
                        job_company=job_company,
                        browser_session=browser_session,
                        require_form_fields=True,
                    )
                    if auto_attached is not None:
                        page, _ = auto_attached
                if page is None:
                    bound_err = exc

            if bound_err is not None or page is None:
                exc_to_report = bound_err or BoundPageInvalidError(
                    "lost_closed",
                    "Nenhuma aba vinculada ou a aba anterior foi fechada.",
                )
                recovery_blocker = {
                    "code": "unknown_origin"
                    if exc_to_report.code in {"origin_changed", "unsafe_origin"}
                    else exc_to_report.code,
                    "message": str(exc_to_report),
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
                        app_e.last_error = str(exc_to_report)
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

            portal_blocker = await self._recheck_live_portal_session(
                session_factory=session_factory,
                user_id=user_id,
                browser_session=browser_session,
                page_url=page.url or "",
                page=page,
            )
            if portal_blocker is not None:
                with session_factory() as write_db:
                    app_w = write_db.scalar(
                        select(Application).where(
                            Application.id == application_id,
                            Application.user_id == user_id,
                        )
                    )
                    if app_w is not None:
                        step_payload = self._redact_step_dict(
                            {
                                "step_id": None,
                                "portal": portal_blocker["portal"],
                                "page_url": page.url or "",
                                "page_origin": "",
                                "heading": "",
                                "open_form_required": False,
                                "final_submit_detected": False,
                                "has_resume_upload": False,
                                "filled": [],
                                "skipped": [],
                                "uploaded_files": [],
                                "blockers": [portal_blocker["blocker"]],
                                "notices": [],
                                "pending_refill": False,
                                "pending_action_ids": [],
                            }
                        )
                        app_w.status = ApplicationStatus.NEEDS_USER.value
                        app_w.last_step_json = step_payload
                        app_w.last_error = portal_blocker["blocker"]["message"]
                        write_db.commit()
                        app_payload = self.get_application(
                            write_db,
                            application_id=application_id,
                            user_id=user_id,
                            browser_session=browser_session,
                        )
                    else:
                        raise ApplicationNotFoundError("Candidatura não encontrada.")
                return {
                    "status": ApplicationStatus.NEEDS_USER.value,
                    "step_id": None,
                    "portal": portal_blocker["portal"],
                    "page_url": app_payload.get("step", {}).get("page_url", ""),
                    "final_submit_detected": False,
                    "filled": [],
                    "skipped": [],
                    "uploaded_files": [],
                    "blockers": [portal_blocker["blocker"]],
                    "notices": [],
                    "pending_action_ids": [],
                    "application": app_payload,
                }

            salary_queue = self._build_pending_queue(
                user_id=user_id,
                application_id=application_id,
                browser_session=browser_session,
            )

            job_locale = self.resume_service.resume_language(job_title, job_description)
            slot_values = build_verified_slot_values(
                doc,
                job_company=job_company,
                job_title=job_title,
                job_description=job_description,
            )

            async def load_arsenal_candidates(snapshot: StepSnapshot) -> dict[str, Any]:
                with session_factory() as db:
                    scoped_desc = f"{job_company} {job_description}".strip() if job_company else job_description
                    context = JobContext.from_job(
                        job_title,
                        scoped_desc,
                        job_locale,
                        [skill.name for skill in doc.skills],
                        seniority,
                        contract,
                    )
                    telemetry = get_engine_telemetry(self.settings)
                    local_gateway = self.resume_service.llm if telemetry.get("mode") == "local_llm" else None
                    matched, _ = await match_arsenal_for_step(
                        db,
                        user_id,
                        snapshot,
                        context,
                        slot_values,
                        gateway=local_gateway,
                    )
                    return matched

            def claim_pending(snapshot: StepSnapshot) -> tuple[dict[str, str], dict[str, str]]:
                with session_factory() as db:
                    return salary_queue.claim_pending(db, snapshot)

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
                    claim_pending=claim_pending,
                    load_arsenal_candidates=load_arsenal_candidates,
                    expected_locale=job_locale,
                    slot_values=slot_values,
                )
                if run_res.snapshot.open_form_required and not run_res.snapshot.fields:
                    handoff = await self._try_auto_attach_matching_tab(
                        application_id=application_id,
                        job_title=job_title,
                        job_company=job_company,
                        browser_session=browser_session,
                        require_form_fields=True,
                        exclude_page=page,
                    )
                    followed = False
                    if handoff is not None:
                        page, _ = handoff
                        followed = True
                    elif await self._try_follow_entry_apply_cta(
                        application_id=application_id,
                        page=page,
                        browser_session=browser_session,
                    ):
                        followed = True
                    if followed:
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
                            claim_pending=claim_pending,
                            load_arsenal_candidates=load_arsenal_candidates,
                            expected_locale=job_locale,
                            slot_values=slot_values,
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
                        operation=str(usage.get("operation") or "arsenal_selection"),
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
                step_payload["salary_actions"] = self._redact_step_dict({
                    "actions": salary_queue.synchronize(
                        write_db,
                        snapshot,
                        {b.field_id for b in run_res.blockers if b.code in {"salary_approval_required", "salary_review_required"} and b.field_id},
                        {str(item["field_id"]) for item in run_res.filled},
                        job,
                        doc=doc,
                        all_blockers=run_res.blockers,
                        skipped_items=run_res.skipped,
                        write_outcomes=run_res.write_outcomes,
                        locale=job_locale,
                    )
                })["actions"]
                step_payload["pending_actions"] = self._redact_step_dict({
                    "items": salary_queue.last_pending_actions
                })["items"]
                step_payload["pending_action_ids"] = list(salary_queue.last_pending_action_ids)
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
                "pending_action_ids": step_payload["pending_action_ids"],
                "application": app_payload,
            }

    def _build_pending_queue(
        self, *, user_id: str, application_id: str, browser_session: BrowserSession
    ) -> SalaryQueue:
        return SalaryQueue(
            self.settings.careerpilot_session_secret,
            user_id,
            application_id,
            browser_session.application_binding_key(application_id),
            browser_generation=browser_session.browser_generation,
        )

    async def _recheck_live_portal_session(
        self,
        *,
        session_factory: Any,
        user_id: str,
        browser_session: BrowserSession,
        page_url: str,
        page: Any,
    ) -> dict[str, Any] | None:
        matched = _match_portal_for_url(page_url, allow_test_origins=browser_session.allow_test_origins)
        if matched is None:
            return None
        portal_key, scope_key = matched
        entry = PORTAL_REGISTRY.get(portal_key)
        if entry is None:
            return None
        probe = await probe_session(
            PortalScope(portal_key=portal_key, scope_key=scope_key, entry_url=page_url),
            browser_session,
            page=page,
        )
        if probe.get("stale_generation"):
            return None
        with session_factory() as db:
            row = db.scalar(
                select(PortalSessionStatusRow).where(
                    PortalSessionStatusRow.user_id == user_id,
                    PortalSessionStatusRow.portal_key == portal_key,
                    PortalSessionStatusRow.scope_key == scope_key,
                )
            )
            now = utc_rfc3339()
            if row is None:
                db.add(
                    PortalSessionStatusRow(
                        user_id=user_id,
                        portal_key=portal_key,
                        scope_key=scope_key,
                        status=str(probe["status"]),
                        reason_code=probe.get("reason_code"),
                        checked_at=probe.get("checked_at"),
                        expires_at=probe.get("expires_at"),
                        revision=1,
                        updated_at=now,
                    )
                )
            else:
                row.status = str(probe["status"])
                row.reason_code = probe.get("reason_code")
                row.checked_at = probe.get("checked_at")
                row.expires_at = probe.get("expires_at")
                row.revision += 1
                row.updated_at = now
            db.commit()

        if probe["status"] == "login_required":
            return {
                "portal": portal_key,
                "blocker": {
                    "code": "login_required",
                    "message": (
                        f"Sessão expirada ou login necessário no portal {entry.name}. "
                        "Entre na conta no Chrome e reverifique antes de preencher."
                    ),
                    "can_answer": False,
                },
            }
        if probe["status"] == "challenge":
            return {
                "portal": portal_key,
                "blocker": {
                    "code": "captcha",
                    "message": (
                        f"Verificação ou desafio anti-bot detectado em {entry.name}. "
                        "Conclua manualmente no Chrome antes de continuar."
                    ),
                    "can_answer": False,
                },
            }
        if probe["status"] == "unavailable":
            return {
                "portal": portal_key,
                "blocker": {
                    "code": "portal_unavailable",
                    "message": f"O portal {entry.name} retornou indisponibilidade temporária ou bloqueio.",
                    "can_answer": False,
                },
            }
        return None

    async def approve_pending_action(
        self,
        action_id: str,
        body: ApprovePendingActionRequest | ApproveSalaryRequest,
        *,
        user_id: str,
        browser_session: BrowserSession,
    ) -> dict[str, Any]:
        """Validate, seal, and CAS-approve a pending manual action with optional Arsenal draft."""
        session_factory = get_session_factory(self.settings)
        with session_factory() as db:
            record = db.scalar(
                select(PendingManualActionRow).where(
                    PendingManualActionRow.id == action_id,
                    PendingManualActionRow.user_id == user_id,
                )
            )
            if record is None:
                raise ApplicationNotFoundError("Pendência manual não encontrada.")
            application_id = record.application_id

        lock = _get_app_op_lock(self.settings.database_url, user_id, application_id)
        if lock.locked():
            raise OperationInProgressError("Outra operação desta candidatura está em andamento.")
        async with lock:
            with session_factory() as db:
                app = db.get(Application, application_id)
                record = db.get(PendingManualActionRow, action_id)
                if app is None or app.user_id != user_id or record is None or record.user_id != user_id:
                    raise ApplicationNotFoundError("Pendência manual não encontrada.")
                job = db.get(JobPosting, app.job_id)
                if record.status != "pending":
                    raise PendingActionConflictError(
                        "action_already_decided",
                        "Esta pendência já foi decidida ou invalidada; não pode ser sobrescrita.",
                        current=serialize_pending_action_row(
                            record,
                            job_title=job.title if job else "",
                            company=job.company if job else "",
                        ),
                    )
                if (
                    app.status not in RESUMABLE_STATUSES
                    or record.revision != body.revision
                    or record.field_fingerprint != body.field_fingerprint
                ):
                    raise PendingActionConflictError(
                        "step_changed",
                        "A pendência ou revisão mudou. Atualize a etapa.",
                        current=serialize_pending_action_row(
                            record,
                            job_title=job.title if job else "",
                            company=job.company if job else "",
                        ),
                    )
                job_url = (job.apply_url or job.job_url) if job else None
                job_title = job.title if job else ""
                job_company = job.company if job else ""

            try:
                page = browser_session.validate_bound_page(application_id, job_url=job_url)
            except BoundPageInvalidError as exc:
                raise PendingActionConflictError(
                    "step_changed", "Vincule novamente a aba da candidatura."
                ) from exc

            snapshot = await inspect_step(
                page,
                navigation_gen=browser_session.get_navigation_generation(page),
                expected_job_title=job_title,
                expected_company=job_company,
            )
            queue = self._build_pending_queue(
                user_id=user_id, application_id=application_id, browser_session=browser_session
            )
            live_field = next(
                (f for f in snapshot.fields if f.field_id == record.field_key and f.editable and not f.has_value),
                None,
            )
            live_fp: str | None = None
            if live_field is not None:
                cur = salary_context(live_field) if record.kind == "salary" else (None, None)
                shp = _field_shape(live_field) if record.kind == "salary" else None
                live_fp = queue.field_fingerprint(
                    queue.step_key(snapshot),
                    live_field.field_id,
                    live_field,
                    currency=cur[0],
                    period=cur[1],
                    shape=shp,
                    kind=record.kind,  # type: ignore[arg-type]
                )
            if (
                snapshot.login_required
                or snapshot.has_otp
                or snapshot.has_captcha
                or snapshot.vacancy_mismatch
                or record.step_key != queue.step_key(snapshot)
                or live_field is None
                or live_fp != record.field_fingerprint
                or (record.kind == "salary" and live_field.category != "salary")
            ):
                with session_factory() as db:
                    now_stale = utc_rfc3339()
                    db.execute(
                        update(PendingManualActionRow)
                        .where(
                            PendingManualActionRow.id == action_id,
                            PendingManualActionRow.user_id == user_id,
                            PendingManualActionRow.status == "pending",
                            PendingManualActionRow.revision == body.revision,
                        )
                        .values(
                            status="stale",
                            approval_sealed=None,
                            revision=body.revision + 1,
                            updated_at=now_stale,
                        )
                    )
                    db.commit()
                    refreshed = db.get(PendingManualActionRow, action_id)
                raise PendingActionConflictError(
                    "step_changed",
                    "O formulário ou opções mudaram no navegador. Atualize a etapa.",
                    current=serialize_pending_action_row(
                        refreshed, job_title=job_title, company=job_company
                    )
                    if refreshed is not None
                    else None,
                )

            save_opts = getattr(body, "save_to_arsenal", None)
            sealed = queue.validate_and_seal(
                record,
                body.approval,
                live_field=live_field,
                save_to_arsenal=save_opts,
            )

            arsenal_draft_row: ArsenalRespostaRow | None = None
            if save_opts is not None:
                raw_val = getattr(body.approval, "value", "")
                if not isinstance(raw_val, str) or not raw_val.strip():
                    raise PendingActionValidationError(
                        "arsenal_requires_text",
                        "Apenas respostas textuais reutilizáveis podem virar rascunho no Arsenal.",
                    )
                try:
                    arsenal_draft_row = build_confirmed_answer_arsenal_draft(
                        user_id=user_id,
                        question_label=live_field.label,
                        question_signature=live_field.question_signature,
                        control_kind=live_field.control_kind,
                        answer_text=raw_val.strip(),
                        language=save_opts.language,
                        category=save_opts.category,
                        role_archetype=save_opts.role_archetype,
                        trigger_keywords=save_opts.trigger_keywords,
                        when_to_use=save_opts.when_to_use,
                        when_not_to_use=save_opts.when_not_to_use,
                        role_tags=save_opts.role_tags,
                        job_company=job_company,
                        job_title=job_title,
                    )
                except ArsenalValidationError as exc:
                    raise PendingActionValidationError(exc.code, exc.message) from exc

            with session_factory() as db:
                now = utc_rfc3339()
                changed = db.execute(
                    update(PendingManualActionRow)
                    .where(
                        PendingManualActionRow.id == action_id,
                        PendingManualActionRow.user_id == user_id,
                        PendingManualActionRow.status == "pending",
                        PendingManualActionRow.revision == body.revision,
                        PendingManualActionRow.field_fingerprint == body.field_fingerprint,
                        PendingManualActionRow.application_id.in_(
                            select(Application.id).where(Application.status.in_(RESUMABLE_STATUSES))
                        ),
                    )
                    .values(
                        status="approved",
                        approval_sealed=sealed,
                        approved_at=now,
                        revision=body.revision + 1,
                        updated_at=now,
                    )
                    .returning(PendingManualActionRow.id)
                ).scalar_one_or_none()
                if changed is None:
                    cur_row = db.get(PendingManualActionRow, action_id)
                    raise PendingActionConflictError(
                        "step_changed",
                        "A pendência mudou antes de gravar a aprovação.",
                        current=serialize_pending_action_row(
                            cur_row, job_title=job_title, company=job_company
                        )
                        if cur_row is not None
                        else None,
                    )

                arsenal_draft_id: str | None = None
                if arsenal_draft_row is not None:
                    arsenal_draft_id = arsenal_draft_row.id
                    db.add(arsenal_draft_row)

                db.add(
                    AuditEvent(
                        actor=user_id,
                        event_type="pending_action_approved",
                        entity_type="pending_manual_action",
                        entity_id=action_id,
                        details={
                            "kind": record.kind,
                            "saved_to_arsenal": arsenal_draft_id is not None,
                        },
                    )
                )
                db.commit()
                updated_row = db.get(PendingManualActionRow, action_id)
                assert updated_row is not None
                return {
                    "approved": True,
                    "arsenal_draft_id": arsenal_draft_id,
                    "action": serialize_pending_action_row(
                        updated_row, job_title=job_title, company=job_company
                    ),
                }

    async def approve_salary(
        self,
        action_id: str,
        body: ApproveSalaryRequest,
        *,
        user_id: str,
        browser_session: BrowserSession,
    ) -> dict[str, Any]:
        return await self.approve_pending_action(
            action_id, body, user_id=user_id, browser_session=browser_session
        )

    async def skip_pending_action(
        self,
        action_id: str,
        body: SkipPendingActionRequest,
        *,
        user_id: str,
    ) -> dict[str, Any]:
        """Skip an optional pending manual action atomically via CAS without touching browser state."""
        session_factory = get_session_factory(self.settings)
        with session_factory() as db:
            record = db.scalar(
                select(PendingManualActionRow).where(
                    PendingManualActionRow.id == action_id,
                    PendingManualActionRow.user_id == user_id,
                )
            )
            if record is None:
                raise ApplicationNotFoundError("Pendência manual não encontrada.")
            application_id = record.application_id

        lock = _get_app_op_lock(self.settings.database_url, user_id, application_id)
        if lock.locked():
            raise OperationInProgressError("Outra operação desta candidatura está em andamento.")
        async with lock:
            with session_factory() as db:
                record = db.get(PendingManualActionRow, action_id)
                app = db.get(Application, application_id)
                if record is None or record.user_id != user_id or app is None or app.user_id != user_id:
                    raise ApplicationNotFoundError("Pendência manual não encontrada.")
                job = db.get(JobPosting, app.job_id)
                job_title = job.title if job else ""
                job_company = job.company if job else ""
                if bool(record.field_json.get("required", True)) or record.kind == "manual_step":
                    raise PendingActionValidationError(
                        "required_field_cannot_skip",
                        "Campos obrigatórios ou etapas manuais no Chrome não podem ser ignorados.",
                    )
                if record.status != "pending":
                    raise PendingActionConflictError(
                        "action_already_decided",
                        "Esta pendência já foi decidida ou invalidada.",
                        current=serialize_pending_action_row(
                            record, job_title=job_title, company=job_company
                        ),
                    )
                if record.revision != body.revision or record.field_fingerprint != body.field_fingerprint:
                    raise PendingActionConflictError(
                        "step_changed",
                        "A revisão da pendência mudou. Atualize a lista.",
                        current=serialize_pending_action_row(
                            record, job_title=job_title, company=job_company
                        ),
                    )

                now = utc_rfc3339()
                changed = db.execute(
                    update(PendingManualActionRow)
                    .where(
                        PendingManualActionRow.id == action_id,
                        PendingManualActionRow.user_id == user_id,
                        PendingManualActionRow.status == "pending",
                        PendingManualActionRow.revision == body.revision,
                        PendingManualActionRow.field_fingerprint == body.field_fingerprint,
                    )
                    .values(
                        status="skipped",
                        approval_sealed=None,
                        revision=body.revision + 1,
                        updated_at=now,
                    )
                    .returning(PendingManualActionRow.id)
                ).scalar_one_or_none()
                if changed is None:
                    cur_row = db.get(PendingManualActionRow, action_id)
                    raise PendingActionConflictError(
                        "step_changed",
                        "A pendência mudou antes de registrar a dispensa.",
                        current=serialize_pending_action_row(
                            cur_row, job_title=job_title, company=job_company
                        )
                        if cur_row is not None
                        else None,
                    )
                db.add(
                    AuditEvent(
                        actor=user_id,
                        event_type="pending_action_skipped",
                        entity_type="pending_manual_action",
                        entity_id=action_id,
                        details={"kind": record.kind},
                    )
                )
                db.commit()
                updated_row = db.get(PendingManualActionRow, action_id)
                assert updated_row is not None
                return {
                    "skipped": True,
                    "action": serialize_pending_action_row(
                        updated_row, job_title=job_title, company=job_company
                    ),
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
                job_description = job.description if job else ""
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

            job_locale = self.resume_service.resume_language(job_title, job_description)
            resolved_updates: dict[str, str] = {}
            profile_entries: list[StandardAnswerEntry] = []
            arsenal_drafts: list[ArsenalRespostaRow] = []
            answered_field_ids: set[str] = set()

            for item in answers:
                blk = answerable_by_id.get(item.field_id)
                if blk is None:
                    raise InvalidStepAnswerError(
                        f"Campo '{item.field_id}' não aceita resposta nesta etapa ou exige ação manual no Chrome."
                    )
                sig = str(blk.get("question_signature") or "")
                label = str(blk.get("label") or item.field_id)
                if classify_field_category(label, "text") == "salary":
                    raise InvalidStepAnswerError("Use a confirmação específica de pretensão salarial.")
                field_type = str(blk.get("field_type") or "text")
                desc = field_desc_by_id.get(item.field_id)
                ctrl_kind = desc.control_kind if desc is not None else field_type
                rule = match_field_rule(
                    label,
                    ctrl_kind,
                    category_hint=desc.category if desc is not None else blk.get("category"),
                )
                if (
                    classify_field_sensitivity(label) == SensitivityLevel.SENSITIVE_PII
                    or rule.source == "manual_only"
                ):
                    raise InvalidStepAnswerError(
                        "Dado sensível exige aprovação isolada na fila de decisões pendentes."
                    )
                if not sig:
                    raise InvalidStepAnswerError(f"Assinatura de pergunta ausente para {label}.")

                canonical_value = item.value.strip()
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
                elif ctrl_kind == "checkbox" or field_type == "boolean":
                    if normalize_label_text(canonical_value) not in {
                        "sim",
                        "yes",
                        "true",
                        "1",
                        "verdadeiro",
                        "nao",
                        "no",
                        "false",
                        "0",
                        "falso",
                    }:
                        raise InvalidStepAnswerError(
                            f"Resposta booleana inválida para '{label}'."
                        )
                else:
                    constraints = (
                        extract_control_constraints(
                            control_kind=ctrl_kind,
                            label=desc.label,
                            locale=job_locale,
                            required=desc.required,
                            locator_hint=desc.locator_hint,
                        )
                        if desc is not None
                        else ControlConstraints(
                            control_kind=ctrl_kind,
                            label=label,
                            locale=job_locale,
                        )
                    )
                    outcome = validate_candidate_value(
                        constraints,
                        canonical_value,
                        rule_id=rule.rule_id,
                        intent=rule.formatter or rule.intent,
                        source_locale=job_locale,
                    )
                    if not outcome.valid or outcome.value is None:
                        raise InvalidStepAnswerError(
                            outcome.blocker_message or f"Valor inválido para '{label}'."
                        )
                    canonical_value = outcome.value

                if item.save_to_arsenal:
                    meta = item.arsenal_meta
                    try:
                        draft_row = build_confirmed_answer_arsenal_draft(
                            user_id=user_id,
                            question_label=label,
                            question_signature=sig,
                            control_kind=ctrl_kind,
                            answer_text=canonical_value,
                            language=meta.language if meta is not None else job_locale,
                            category=meta.category if meta is not None else "custom",
                            role_archetype=meta.role_archetype if meta is not None else "all",
                            trigger_keywords=meta.trigger_keywords if meta is not None else (),
                            when_to_use=meta.when_to_use if meta is not None else (),
                            when_not_to_use=meta.when_not_to_use if meta is not None else (),
                            role_tags=meta.role_tags if meta is not None else (),
                            job_company=job_company,
                            job_title=job_title,
                        )
                    except (ArsenalValidationError, ValueError) as exc:
                        detail_msg = exc.message if isinstance(exc, ArsenalValidationError) else str(exc)
                        raise InvalidStepAnswerError(
                            f"Esta resposta pode ser usada na etapa, mas não pode ser salva no arsenal: {detail_msg}"
                        ) from exc
                    arsenal_drafts.append(draft_row)

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

                now_utc = utc_rfc3339()
                if answered_field_ids:
                    write_db.execute(
                        update(PendingManualActionRow)
                        .where(
                            PendingManualActionRow.user_id == user_id,
                            PendingManualActionRow.application_id == application_id,
                            PendingManualActionRow.field_key.in_(sorted(answered_field_ids)),
                            PendingManualActionRow.status == "pending",
                            PendingManualActionRow.kind != "salary",
                        )
                        .values(
                            status="stale",
                            approval_sealed=None,
                            updated_at=now_utc,
                            revision=PendingManualActionRow.revision + 1,
                        )
                    )

                remaining_blockers = [
                    blk
                    for blk in list(persisted_step.get("blockers") or [])
                    if str(blk.get("field_id") or "") not in answered_field_ids
                ]
                remaining_pending_actions = [
                    act
                    for act in list(persisted_step.get("pending_actions") or [])
                    if str(act.get("field_id") or "") not in answered_field_ids
                    or str(act.get("category") or "") == "salary"
                ]
                updated_step = dict(persisted_step)
                updated_step["blockers"] = remaining_blockers
                updated_step["pending_actions"] = remaining_pending_actions
                updated_step["pending_action_ids"] = [
                    str(act["id"]) for act in remaining_pending_actions if act.get("id")
                ]
                updated_step["pending_refill"] = True
                app_w.last_step_json = self._redact_step_dict(updated_step)
                app_w.last_error = remaining_blockers[0]["message"] if remaining_blockers else None

                write_db.add_all(arsenal_drafts)
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
                "arsenal_draft_count": len(arsenal_drafts),
                "step_id": step_id,
                "profile_revision": new_profile_rev,
                "application": app_payload,
            }
