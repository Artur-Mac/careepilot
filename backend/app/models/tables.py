"""SQLAlchemy 2 ORM models for CareerPilot (SQLite WAL + Postgres-ready types)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _uuid() -> str:
    return str(uuid.uuid4())


class UserSettings(Base):
    __tablename__ = "user_settings"

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
    kind: Mapped[str] = mapped_column(String(32))  # ats | job_board | search | email | manual
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    mode: Mapped[str] = mapped_column(String(32), default="mocked")  # mocked | live | credential_required | manual_only
    rate_limit_rpm: Mapped[int] = mapped_column(Integer, default=30)
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
    board_token: Mapped[str | None] = mapped_column(String(120), nullable=True)
    is_fictional_test: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class JobPosting(Base):
    __tablename__ = "job_postings"
    __table_args__ = (
        UniqueConstraint("canonical_url", name="uq_job_canonical_url"),
        Index("ix_job_dedup_sig", "dedup_signature"),
        Index("ix_job_discovered_at", "discovered_at"),
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
    apply_mode: Mapped[str] = mapped_column(String(32), default="manual_only")
    source_attribution: Mapped[str] = mapped_column(String(300))
    injection_signals: Mapped[list[str]] = mapped_column(JSON, default=list)

    requirements: Mapped[list[JobRequirement]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
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
    is_mocked: Mapped[bool] = mapped_column(Boolean, default=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    job: Mapped[JobPosting] = relationship(back_populates="source_records")


class JobRequirement(Base):
    __tablename__ = "job_requirements"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("job_postings.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(24))  # required | preferred
    term: Mapped[str] = mapped_column(String(120))
    snippet: Mapped[str] = mapped_column(String(400))

    job: Mapped[JobPosting] = relationship(back_populates="requirements")


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

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("job_postings.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(String(64), default="local-applicant")
    profile_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    job_description_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pdf_path: Mapped[str] = mapped_column(String(512))
    docx_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
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


class SchedulerRun(Base):
    __tablename__ = "scheduler_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    status: Mapped[str] = mapped_column(String(32))  # running | paused | completed | failed | stopped
    jobs_discovered: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
