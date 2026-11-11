"""Frozen data contracts and API DTOs (Pydantic v2)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from backend.app.core.ssrf import validate_external_url

WorkplaceType = Literal["remote", "hybrid", "onsite", "unknown"]
ApplyMode = Literal["automated_allowed", "assisted_manual_submit", "manual_only", "unsupported"]


class NormalizedJob(BaseModel):
    """Frozen NormalizedJob DTO shared between architecture and implementation."""

    source: str = Field(min_length=1, max_length=64)
    source_job_id: str | None = Field(default=None, max_length=256)
    company: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=240)
    location: str | None = Field(default=None, max_length=200)
    workplace_type: WorkplaceType = "unknown"
    description: str = Field(min_length=1, max_length=50000)
    job_url: str = Field(min_length=8, max_length=1024)
    apply_url: str | None = Field(default=None, max_length=1024)
    posted_at: datetime | None = None
    discovered_at: datetime
    employment_type: str | None = Field(default=None, max_length=64)
    seniority: str | None = Field(default=None, max_length=64)
    salary_min: float | None = Field(default=None, ge=0)
    salary_max: float | None = Field(default=None, ge=0)
    salary_currency: str | None = Field(default=None, max_length=16)
    apply_mode: ApplyMode = "manual_only"
    source_attribution: str = Field(min_length=1, max_length=300)

    @field_validator("job_url")
    @classmethod
    def _validate_job_url(cls, value: str, info: ValidationInfo) -> str:
        from backend.app.core.config import get_settings

        settings = get_settings()
        ctx = info.context if isinstance(info.context, dict) else {}
        allow_test: set[str] = set(ctx.get("allow_test_origins") or set())
        if settings.test_ats_origin:
            allow_test.add(settings.test_ats_origin)
        return validate_external_url(value, allow_test_origins=allow_test)

    @field_validator("apply_url")
    @classmethod
    def _validate_apply_url(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None or not value.strip():
            return None
        from backend.app.core.config import get_settings

        settings = get_settings()
        ctx = info.context if isinstance(info.context, dict) else {}
        allow_test: set[str] = set(ctx.get("allow_test_origins") or set())
        if settings.test_ats_origin:
            allow_test.add(settings.test_ats_origin)
        return validate_external_url(value, allow_test_origins=allow_test)

    @model_validator(mode="after")
    def _check_salary_range(self) -> NormalizedJob:
        if (
            self.salary_min is not None
            and self.salary_max is not None
            and self.salary_max < self.salary_min
        ):
            self.salary_min, self.salary_max = self.salary_max, self.salary_min
        return self


class SearchQuery(BaseModel):
    """Frozen SearchQuery DTO."""

    text: str = Field(default="data engineer", min_length=1, max_length=300)
    location: str | None = Field(default=None, max_length=120)
    remote_only: bool = False
    limit: int = Field(default=50, ge=1, le=200)


class BrowserAttemptResult(BaseModel):
    """Frozen BrowserDriver result contract."""

    status: str
    fields_detected: list[str] = Field(default_factory=list)
    fields_filled: dict[str, str] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    final_url: str
    confirmed_submission: bool = False


class UnlockRequest(BaseModel):
    passcode: str = Field(min_length=1, max_length=200)


class DiscoverRequest(BaseModel):
    text: str = Field(default="data engineer python sql", min_length=1, max_length=300)
    location: str | None = None
    remote_only: bool = False
    limit: int = Field(default=50, ge=1, le=200)
    sources: list[str] | None = None
    use_live_network: bool = False


class PrepareApplicationRequest(BaseModel):
    job_id: str
    custom_answers: dict[str, str] = Field(default_factory=dict)


class ApprovalActionRequest(BaseModel):
    comment: str | None = Field(default=None, max_length=1000)


class ExecuteApplicationRequest(BaseModel):
    submit: bool = False
    allow_visual_fallback: bool = False


class ManualConfirmRequest(BaseModel):
    confirmation_reference: str = Field(min_length=3, max_length=200)
    confirmation_url: str | None = Field(default=None, max_length=1024)
    notes: str | None = Field(default=None, max_length=1000)


class AnswerQuestionsRequest(BaseModel):
    answers: dict[str, str]


class SettingsUpdateRequest(BaseModel):
    target_roles: list[str] | None = None
    preferred_locations: list[str] | None = None
    workplace_preferences: list[str] | None = None
    seniority_preferences: list[str] | None = None
    contract_types: list[str] | None = None
    languages: list[str] | None = None
    excluded_companies: list[str] | None = None
    salary_floor: float | None = Field(default=None, ge=0)
    salary_currency: str | None = None
    travel_tolerance: str | None = None
    availability: str | None = None
    work_authorization: str | None = None
    dry_run: bool | None = None
    live_mode_enabled: bool | None = None
    notification_channel: str | None = None
    scheduler_enabled: bool | None = None
    scheduler_interval_hours: int | None = Field(default=None, ge=1, le=24)
