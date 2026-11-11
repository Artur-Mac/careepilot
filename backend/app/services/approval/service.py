"""Immutable approval packet builder, 1-use transactional decision engine, and pre-submit diff check."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from backend.app.core.state_machine import ApplicationStatus, validate_transition
from backend.app.models.tables import (
    AnswerVersion,
    Application,
    ApprovalDecision,
    ApprovalRequest,
    AuditEvent,
    JobPosting,
    MatchEvaluation,
    ResumeVersion,
)
from backend.app.services.resume.service import sha256_file, sha256_text


class ApprovalError(ValueError):
    """Raised when an approval is missing, expired, replayed, rejected, or tampered."""


def compute_packet_hash(
    *,
    job_id: str,
    employer_name: str,
    canonical_apply_url: str,
    policy_mode: str,
    description_sha256: str,
    resume_sha256: str,
    answers_sha256: str,
    user_id: str,
    nonce: str,
    expires_at_iso: str,
) -> str:
    payload = {
        "job_id": job_id,
        "employer_name": employer_name,
        "canonical_apply_url": canonical_apply_url,
        "policy_mode": policy_mode,
        "description_sha256": description_sha256,
        "resume_sha256": resume_sha256,
        "answers_sha256": answers_sha256,
        "user_id": user_id,
        "nonce": nonce,
        "expires_at": expires_at_iso,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ApprovalService:
    """Enforces 1-use human approval and mandatory pre-submit diff verification."""

    def create_approval_request(
        self,
        db: Session,
        *,
        application: Application,
        job: JobPosting,
        resume: ResumeVersion,
        answers: AnswerVersion,
        match: MatchEvaluation | None,
        ttl_hours: int = 24,
    ) -> ApprovalRequest:
        expires_at = datetime.now(UTC) + timedelta(hours=ttl_hours)
        nonce = secrets.token_hex(16)
        apply_url = job.apply_url or job.job_url
        packet_hash = compute_packet_hash(
            job_id=job.id,
            employer_name=job.company,
            canonical_apply_url=apply_url,
            policy_mode=job.apply_mode,
            description_sha256=job.description_hash,
            resume_sha256=resume.pdf_sha256,
            answers_sha256=answers.answers_sha256,
            user_id=application.user_id,
            nonce=nonce,
            expires_at_iso=expires_at.isoformat(),
        )

        missing_items = [q["question_label"] for q in (answers.pending_questions or [])]
        completeness = {
            "resume_readable": bool(resume.extracted_text),
            "pending_questions_count": len(missing_items),
            "policy_mode": job.apply_mode,
            "injection_signals": list(job.injection_signals or []),
        }
        fit_reasons = [match.explanation] if match else ["Avaliação pendente"]

        req = ApprovalRequest(
            application_id=application.id,
            job_id=job.id,
            user_id=application.user_id,
            employer_name=job.company,
            canonical_apply_url=apply_url,
            policy_mode=job.apply_mode,
            description_sha256=job.description_hash,
            resume_sha256=resume.pdf_sha256,
            answers_sha256=answers.answers_sha256,
            packet_hash=packet_hash,
            completeness_report=completeness,
            missing_items=missing_items,
            fit_reasons=fit_reasons,
            nonce=nonce,
            expires_at=expires_at,
        )
        db.add(req)
        db.flush()
        return req

    def record_decision(
        self,
        db: Session,
        *,
        application_id: str,
        decision: str,
        decided_by: str = "local-applicant",
        comment: str | None = None,
    ) -> tuple[Application, ApprovalRequest, ApprovalDecision]:
        """Atomically record 1-use approval/rejection and transition application state."""
        if decision not in {"approved", "rejected", "change_requested"}:
            raise ApprovalError(f"Decisão inválida: {decision}")

        app = db.scalar(select(Application).where(Application.id == application_id))
        if app is None:
            raise ApprovalError("Candidatura não encontrada.")

        req = db.scalar(
            select(ApprovalRequest)
            .where(ApprovalRequest.application_id == application_id)
            .order_by(ApprovalRequest.created_at.desc())
        )
        if req is None:
            raise ApprovalError("Pacote de aprovação não encontrado.")

        now = datetime.now(UTC)
        exp = req.expires_at.replace(tzinfo=UTC) if req.expires_at.tzinfo is None else req.expires_at
        if exp < now:
            if app.status == ApplicationStatus.AWAITING_APPROVAL.value:
                app.status = ApplicationStatus.EXPIRED.value
                app.version += 1
                db.commit()
            raise ApprovalError("Solicitação de aprovação expirada; gere um novo pacote.")

        existing_dec = db.scalar(
            select(ApprovalDecision).where(ApprovalDecision.approval_request_id == req.id)
        )
        if existing_dec is not None:
            raise ApprovalError("Decisão já registrada para este pacote (replay bloqueado).")

        if decision == "approved":
            self.verify_packet_integrity(db, app=app, req=req, check_consumed=True)
            target_status = ApplicationStatus.APPROVED
        elif decision == "rejected":
            target_status = ApplicationStatus.REJECTED
        else:
            target_status = ApplicationStatus.NEEDS_USER_INPUT

        validate_transition(app.status, target_status, allow_idempotent=False)

        # Optimistic concurrency update ensures single-use transition safety
        upd = cast(
            CursorResult[Any],
            db.execute(
                update(Application)
                .where(Application.id == app.id, Application.version == app.version)
                .values(status=target_status.value, version=app.version + 1, updated_at=now)
            ),
        )
        if upd.rowcount != 1:
            db.rollback()
            raise ApprovalError("Conflito concorrente ao atualizar estado da candidatura.")

        dec = ApprovalDecision(
            approval_request_id=req.id,
            application_id=app.id,
            decision=decision,
            decided_by=decided_by,
            comment=comment,
            decided_at=now,
        )
        db.add(dec)
        db.add(
            AuditEvent(
                actor=decided_by,
                event_type=f"approval_{decision}",
                entity_type="application",
                entity_id=app.id,
                details={
                    "approval_request_id": req.id,
                    "packet_hash": req.packet_hash,
                    "comment": comment,
                },
            )
        )
        db.commit()
        db.refresh(app)
        return app, req, dec

    def verify_packet_integrity(
        self,
        db: Session,
        *,
        app: Application,
        req: ApprovalRequest,
        check_consumed: bool = True,
    ) -> tuple[JobPosting, ResumeVersion, AnswerVersion]:
        """Mandatory pre-submit diff check: reject if expired, consumed, or any artifact mutated."""
        now = datetime.now(UTC)
        exp = req.expires_at.replace(tzinfo=UTC) if req.expires_at.tzinfo is None else req.expires_at
        if exp < now:
            raise ApprovalError("Aprovação expirada antes da execução.")
        if check_consumed and req.consumed:
            raise ApprovalError("Aprovação já consumida (uso único obrigatório).")

        job = db.scalar(select(JobPosting).where(JobPosting.id == app.job_id))
        resume = db.scalar(select(ResumeVersion).where(ResumeVersion.id == app.resume_version_id))
        answers = db.scalar(select(AnswerVersion).where(AnswerVersion.id == app.answer_version_id))
        if not job or not resume or not answers:
            raise ApprovalError("Artefatos do pacote aprovado estão incompletos.")

        if job.description_hash != req.description_sha256:
            raise ApprovalError("Hash da descrição da vaga mudou após preparação (re-aprovação exigida).")

        pdf_file = Path(resume.pdf_path)
        if not pdf_file.exists():
            raise ApprovalError("Arquivo PDF aprovado não encontrado no disco.")
        disk_pdf_sha = sha256_file(pdf_file)
        if disk_pdf_sha != req.resume_sha256 or resume.pdf_sha256 != req.resume_sha256:
            raise ApprovalError("Hash SHA-256 do currículo PDF divergiu do pacote aprovado!")

        current_answers_sha = sha256_text(
            json.dumps(answers.answers_json, sort_keys=True, ensure_ascii=False)
        )
        if current_answers_sha != req.answers_sha256 or answers.answers_sha256 != req.answers_sha256:
            raise ApprovalError("Hash SHA-256 das respostas divergiu do pacote aprovado!")

        recomputed_packet = compute_packet_hash(
            job_id=job.id,
            employer_name=req.employer_name,
            canonical_apply_url=req.canonical_apply_url,
            policy_mode=req.policy_mode,
            description_sha256=req.description_sha256,
            resume_sha256=disk_pdf_sha,
            answers_sha256=current_answers_sha,
            user_id=req.user_id,
            nonce=req.nonce,
            expires_at_iso=exp.isoformat(),
        )
        if recomputed_packet != req.packet_hash:
            raise ApprovalError("Integridade do pacote de aprovação violada (hash mismatch).")

        return job, resume, answers

    def consume_approval_atomically(self, db: Session, req: ApprovalRequest) -> None:
        """Mark one-time approval consumed inside a transaction."""
        now = datetime.now(UTC)
        res = cast(
            CursorResult[Any],
            db.execute(
                update(ApprovalRequest)
                .where(ApprovalRequest.id == req.id, ApprovalRequest.consumed.is_(False))
                .values(consumed=True, consumed_at=now)
            ),
        )
        if res.rowcount != 1:
            db.rollback()
            raise ApprovalError("Aprovação já foi consumida anteriormente (replay bloqueado).")
        db.flush()
