"""SQLAlchemy 2 ORM models for CareerPilot (SQLite WAL + Postgres-ready types)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    text,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.core.time import utc_rfc3339
from backend.app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _uuid() -> str:
    return str(uuid.uuid4())


class JsonText(TypeDecorator[Any]):
    """Store JSON objects or arrays as validated SQLite TEXT."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def process_result_value(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            return value
        return json.loads(value)


class UserSettings(Base):
    __tablename__ = "user_settings"
    __table_args__ = (
        CheckConstraint(
            "json_valid(salary_policy_json) AND json_type(salary_policy_json) = 'array'",
            name="ck_user_settings_salary_policy_json",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), unique=True, default="local-applicant")
    target_roles: Mapped[list[str]] = mapped_column(JSON, default=list)
    preferred_locations: Mapped[list[str]] = mapped_column(JSON, default=list)
    workplace_preferences: Mapped[list[str]] = mapped_column(JSON, default=lambda: ["remote", "hybrid"])
    seniority_preferences: Mapped[list[str]] = mapped_column(
        JSON, default=lambda: ["junior", "entry_level", "trainee", "intern"]
    )
    contract_types: Mapped[list[str]] = mapped_column(JSON, default=lambda: ["CLT", "PJ", "full_time"])
    languages: Mapped[list[str]] = mapped_column(JSON, default=lambda: ["pt-BR", "en"])
    excluded_companies: Mapped[list[str]] = mapped_column(JSON, default=list)
    salary_floor: Mapped[float | None] = mapped_column(Float, nullable=True)
    salary_currency: Mapped[str] = mapped_column(String(8), default="BRL")
    salary_policy_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JsonText, nullable=False, default=list, server_default="[]"
    )
    travel_tolerance: Mapped[str] = mapped_column(String(32), default="low")
    availability: Mapped[str] = mapped_column(String(64), default="")
    work_authorization: Mapped[str] = mapped_column(String(64), default="")
    notification_channel: Mapped[str] = mapped_column(String(32), default="local_outbox")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class SearchPlan(Base):
    __tablename__ = "search_plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(120))
    query_text: Mapped[str] = mapped_column(String(300))
    location: Mapped[str | None] = mapped_column(String(120), nullable=True)
    remote_only: Mapped[bool] = mapped_column(Boolean, default=False)
    generated_queries: Mapped[list[str]] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SourceConnector(Base):
    __tablename__ = "source_connectors"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(32), default="browser")  # browser | email
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    mode: Mapped[str] = mapped_column(String(32), default="browser")
    rate_limit_rpm: Mapped[int] = mapped_column(Integer, default=20)
    last_status: Mapped[str] = mapped_column(String(32), default="idle")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    total_fetched: Mapped[int] = mapped_column(Integer, default=0)


class Employer(Base):
    __tablename__ = "employers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    domain: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ats_platform: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class JobPosting(Base):
    __tablename__ = "job_postings"
    __table_args__ = (
        UniqueConstraint("canonical_url", name="uq_job_canonical_url"),
        Index("ix_job_dedup_sig", "dedup_signature"),
        Index("ix_job_discovered_at", "discovered_at"),
        CheckConstraint(
            "json_valid(requirements_json) AND json_type(requirements_json) = 'array'",
            name="ck_job_postings_requirements_json",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    employer_id: Mapped[str | None] = mapped_column(ForeignKey("employers.id"), nullable=True)
    canonical_url: Mapped[str] = mapped_column(String(1024))
    dedup_signature: Mapped[str] = mapped_column(String(256))
    source: Mapped[str] = mapped_column(String(64))
    source_job_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    company: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(240))
    location: Mapped[str | None] = mapped_column(String(200), nullable=True)
    workplace_type: Mapped[str] = mapped_column(String(32), default="unknown")
    description: Mapped[str] = mapped_column(Text)
    summary_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    eligibility_json: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=lambda: {
            "remote_scope": "unspecified",
            "brazil_eligible": "unspecified",
            "reason": None,
        },
    )
    requirements_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JsonText, nullable=False, default=list, server_default="[]"
    )
    description_hash: Mapped[str] = mapped_column(String(64))
    job_url: Mapped[str] = mapped_column(String(1024))
    apply_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_expired: Mapped[bool] = mapped_column(Boolean, default=False)
    employment_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    seniority: Mapped[str | None] = mapped_column(String(64), nullable=True)
    salary_min: Mapped[float | None] = mapped_column(Float, nullable=True)
    salary_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    salary_currency: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source_attribution: Mapped[str] = mapped_column(String(300))
    injection_signals: Mapped[list[str]] = mapped_column(JSON, default=list)

    source_records: Mapped[list[JobSourceRecord]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class JobSourceRecord(Base):
    __tablename__ = "job_source_records"
    __table_args__ = (
        UniqueConstraint("source", "provider_job_id", name="uq_source_provider_job_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("job_postings.id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(String(64))
    provider_job_id: Mapped[str] = mapped_column(String(256))
    raw_url: Mapped[str] = mapped_column(String(1024))
    attribution: Mapped[str] = mapped_column(String(300))
    discovery_channel: Mapped[str] = mapped_column(String(32), nullable=False, default="browser")
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    job: Mapped[JobPosting] = relationship(back_populates="source_records")


class JobDismissal(Base):
    __tablename__ = "job_dismissals"
    __table_args__ = (
        UniqueConstraint("user_id", "job_id", name="uq_job_dismissal_user_job"),
        Index("ix_job_dismissal_user_created", "user_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, default="local-applicant")
    job_id: Mapped[str] = mapped_column(
        ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class DiscoveryRun(Base):
    __tablename__ = "discovery_runs"
    __table_args__ = (
        Index("ix_discovery_run_user_started", "user_id", "started_at"),
        Index("ix_discovery_run_status", "status"),
        CheckConstraint(
            "plan_json IS NULL OR (json_valid(plan_json) AND json_type(plan_json) = 'object')",
            name="ck_discovery_runs_plan_json",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, default="local-applicant")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    query_text: Mapped[str] = mapped_column(String(300), nullable=False)
    queries_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    sources_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    source_errors_json: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False, default=dict)
    plan_json: Mapped[dict[str, Any] | None] = mapped_column(JsonText, nullable=True)
    total_raw: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    inserted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deduped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected_restricted_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    matched_job_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MasterProfile(Base):
    __tablename__ = "master_profiles"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True, default="local-applicant")
    document: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class ResumeVersion(Base):
    __tablename__ = "resume_versions"
    __table_args__ = (
        CheckConstraint(
            "artifact_key IS NULL OR length(artifact_key) = 64",
            name="ck_resume_versions_artifact_key",
        ),
        Index(
            "uq_resume_artifact_v2",
            "user_id",
            "job_id",
            "artifact_key",
            unique=True,
            sqlite_where=text("artifact_key IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("job_postings.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(String(64), default="local-applicant")
    profile_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    job_description_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pdf_path: Mapped[str] = mapped_column(String(512))
    docx_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    markdown_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    pdf_sha256: Mapped[str] = mapped_column(String(64))
    extracted_text: Mapped[str] = mapped_column(Text)
    structured_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    provenance_report: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    diff_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (
        Index("ix_app_status", "status"),
        Index("ix_app_job_user", "job_id", "user_id"),
        Index("uq_applications_id_user_v2", "id", "user_id", unique=True),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("job_postings.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(String(64), default="local-applicant")
    status: Mapped[str] = mapped_column(String(32), default="ready")
    resume_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("resume_versions.id"), nullable=True
    )
    answers_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    last_step_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    channel: Mapped[str] = mapped_column(String(32))
    recipient: Mapped[str] = mapped_column(String(200))
    subject: Mapped[str] = mapped_column(String(256))
    body_preview: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_created_at", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    actor: Mapped[str] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(96))
    entity_type: Mapped[str] = mapped_column(String(64))
    entity_id: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class LlmUsageEvent(Base):
    __tablename__ = "llm_usage_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    operation: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(120))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class PortalSessionStatusRow(Base):
    __tablename__ = "portal_session_status"
    __table_args__ = (
        CheckConstraint(
            "status IN ('unknown','checking','authenticated','login_required','public_access','challenge','unavailable','disconnected')",
            name="ck_portal_session_status_value",
        ),
        CheckConstraint("revision >= 1", name="ck_portal_session_revision"),
        CheckConstraint(
            "status NOT IN ('authenticated','public_access','login_required','challenge') "
            "OR (browser_generation IS NOT NULL AND checked_at IS NOT NULL AND expires_at > checked_at AND expires_at IS NOT NULL)",
            name="ck_portal_session_fresh_evidence",
        ),
    )

    user_id: Mapped[str] = mapped_column(Text, primary_key=True, default="local-applicant")
    portal_key: Mapped[str] = mapped_column(Text, primary_key=True)
    scope_key: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="unknown")
    reason_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    browser_generation: Mapped[str | None] = mapped_column(Text, nullable=True)
    checked_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class PendingManualActionRow(Base):
    __tablename__ = "pending_manual_actions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["application_id", "user_id"],
            ["applications.id", "applications.user_id"],
            ondelete="CASCADE",
            name="fk_pending_action_app_user",
        ),
        CheckConstraint("length(field_fingerprint) = 64", name="ck_pending_fingerprint_len"),
        CheckConstraint(
            "kind IN ('salary','sensitive','ambiguous','low_confidence','invalid','manual_step')",
            name="ck_pending_kind",
        ),
        CheckConstraint(
            "status IN ('pending','approved','applied','skipped','stale')",
            name="ck_pending_status",
        ),
        CheckConstraint(
            "json_valid(field_json) AND json_type(field_json) = 'object'",
            name="ck_pending_field_json",
        ),
        CheckConstraint(
            "suggestion_json IS NULL OR (json_valid(suggestion_json) AND json_type(suggestion_json) = 'object')",
            name="ck_pending_suggestion_json",
        ),
        CheckConstraint("revision >= 1", name="ck_pending_revision"),
        CheckConstraint(
            "status <> 'approved' OR (approval_sealed IS NOT NULL AND approved_at IS NOT NULL)",
            name="ck_pending_approved_sealed",
        ),
        CheckConstraint(
            "status <> 'applied' OR (approved_at IS NOT NULL AND applied_at IS NOT NULL)",
            name="ck_pending_applied_timestamps",
        ),
        CheckConstraint(
            "applied_at IS NULL OR status IN ('applied','stale')",
            name="ck_pending_applied_at_state",
        ),
        CheckConstraint(
            "status NOT IN ('pending','skipped','stale','applied') OR approval_sealed IS NULL",
            name="ck_pending_unapproved_no_secret",
        ),
        Index(
            "uq_pending_action_active_field_v2",
            "user_id",
            "application_id",
            "step_key",
            "field_key",
            unique=True,
            sqlite_where=text("status IN ('pending','approved','applied','skipped')"),
        ),
        Index("ix_pending_action_inbox_v2", "user_id", "status", "updated_at", "id"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(Text, nullable=False, default="local-applicant")
    application_id: Mapped[str] = mapped_column(Text, nullable=False)
    step_key: Mapped[str] = mapped_column(Text, nullable=False)
    field_key: Mapped[str] = mapped_column(Text, nullable=False)
    field_fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    field_json: Mapped[dict[str, Any]] = mapped_column(JsonText, nullable=False)
    suggestion_json: Mapped[dict[str, Any] | None] = mapped_column(JsonText, nullable=True)
    approval_sealed: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    approved_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    applied_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utc_rfc3339)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False, default=utc_rfc3339)


class ArsenalRespostaRow(Base):
    __tablename__ = "arsenal_respostas"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_arsenal_revision"),
        CheckConstraint("status IN ('draft','approved','retired')", name="ck_arsenal_status"),
        CheckConstraint("language IN ('pt-BR','en')", name="ck_arsenal_language"),
        CheckConstraint(
            "role_archetype IN ('all','data_engineer','backend_python','ml_engineer','intern_junior')",
            name="ck_arsenal_archetype",
        ),
        CheckConstraint(
            "category IN ('professional_summary','why_company','technical_challenge','finops_cloud_impact','availability','work_authorization','english_proficiency','salary_note','custom')",
            name="ck_arsenal_category",
        ),
        CheckConstraint(
            "json_valid(payload_json) AND json_type(payload_json) = 'object'",
            name="ck_arsenal_payload_json",
        ),
        CheckConstraint(
            "status <> 'approved' OR approved_at IS NOT NULL",
            name="ck_arsenal_approved_at",
        ),
        Index(
            "uq_arsenal_active_revision_v2",
            "user_id",
            "id",
            unique=True,
            sqlite_where=text("status = 'approved'"),
        ),
        Index(
            "ix_arsenal_match_v2",
            "user_id",
            "status",
            "language",
            "role_archetype",
            "category",
        ),
    )

    user_id: Mapped[str] = mapped_column(Text, primary_key=True, default="local-applicant")
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    language: Mapped[str] = mapped_column(Text, nullable=False, default="pt-BR")
    role_archetype: Mapped[str] = mapped_column(Text, nullable=False, default="all")
    category: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JsonText, nullable=False)
    approved_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utc_rfc3339)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False, default=utc_rfc3339)


class ProfileChangeProposalRow(Base):
    __tablename__ = "profile_change_proposals"
    __table_args__ = (
        CheckConstraint("base_revision >= 0", name="ck_proposal_base_revision"),
        CheckConstraint(
            "status IN ('draft','applied','rejected','stale')",
            name="ck_proposal_status",
        ),
        CheckConstraint("expires_at > created_at", name="ck_proposal_expires_gt_created"),
        CheckConstraint(
            "(status = 'draft' AND proposal_sealed IS NOT NULL AND resolved_at IS NULL) "
            "OR (status <> 'draft' AND proposal_sealed IS NULL AND resolved_at IS NOT NULL)",
            name="ck_proposal_resolution_state",
        ),
        Index("ix_profile_proposals_user_v2", "user_id", "status", "expires_at"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(Text, nullable=False, default="local-applicant")
    base_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="draft")
    proposal_sealed: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utc_rfc3339)
    expires_at: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_at: Mapped[str | None] = mapped_column(Text, nullable=True)


class JobDiscoveryFeedbackRow(Base):
    __tablename__ = "job_discovery_feedback"
    __table_args__ = (
        CheckConstraint(
            "action IN ('high_fit','too_senior','wrong_tech_stack','onsite_outside_recife','blacklist_company')",
            name="ck_feedback_action",
        ),
        CheckConstraint("operation IN ('apply','undo')", name="ck_feedback_operation"),
        CheckConstraint(
            "json_valid(evidence_json) AND json_type(evidence_json) = 'object'",
            name="ck_feedback_evidence_json",
        ),
        UniqueConstraint("user_id", "id", name="uq_feedback_user_id"),
        UniqueConstraint("user_id", "dedup_key", name="uq_feedback_user_dedup"),
        Index(
            "ix_feedback_projection_v2",
            "user_id",
            "job_id",
            "action",
            "created_at",
            "id",
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(Text, nullable=False, default="local-applicant")
    job_id: Mapped[str] = mapped_column(
        Text, ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str] = mapped_column(Text, nullable=False)
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    dedup_key: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JsonText, nullable=False)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utc_rfc3339)


class SearchTuningRuleRow(Base):
    __tablename__ = "search_tuning_rules"
    __table_args__ = (
        CheckConstraint("revision >= 1", name="ck_tuning_revision"),
        CheckConstraint(
            "json_valid(rules_json) AND json_type(rules_json) = 'object'",
            name="ck_tuning_rules_json",
        ),
    )

    user_id: Mapped[str] = mapped_column(Text, primary_key=True, default="local-applicant")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    rules_json: Mapped[dict[str, Any]] = mapped_column(JsonText, nullable=False)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False, default=utc_rfc3339)
