"""Frozen data contracts and API DTOs (Pydantic v2)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from backend.app.core.ssrf import validate_external_url

WorkplaceType = Literal["remote", "hybrid", "onsite", "unknown"]
BrazilEligibilityStatus = Literal["eligible", "restricted", "unspecified"]


class EligibilityInfo(BaseModel):
    remote_scope: str = "unspecified"
    brazil_eligible: BrazilEligibilityStatus = "unspecified"
    reason: str | None = None


class NormalizedJob(BaseModel):
    """NormalizedJob DTO shared across browser collectors and persistence."""

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
    source_attribution: str = Field(min_length=1, max_length=300)
    discovery_channel: str = Field(default="browser", max_length=32)

    @field_validator("job_url")
    @classmethod
    def _validate_job_url(cls, value: str, info: ValidationInfo) -> str:
        origins = info.context.get("allow_test_origins") if isinstance(info.context, dict) else None
        return validate_external_url(value, allow_test_origins=origins)

    @field_validator("apply_url")
    @classmethod
    def _validate_apply_url(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None or not value.strip():
            return None
        origins = info.context.get("allow_test_origins") if isinstance(info.context, dict) else None
        return validate_external_url(value, allow_test_origins=origins)

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
    """Search query DTO for discovery passes."""

    text: str = Field(default="data engineer", min_length=1, max_length=300)
    location: str | None = Field(default=None, max_length=120)
    remote_only: bool = False
    include_remote: bool = True
    limit: int = Field(default=50, ge=1, le=200)


class UnlockRequest(BaseModel):
    passcode: str = Field(min_length=1, max_length=200)


class DiscoverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role_override: str | None = Field(default=None, max_length=160)
    location: str | None = Field(default=None, max_length=120)
    remote_only: bool = False
    include_remote: bool = True
    sources: list[str] | None = None
    limit_per_source: int = Field(default=15, ge=1, le=50)


class PrepareApplicationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1, max_length=64)


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
    notification_channel: str | None = None


class AttachPageRequest(BaseModel):
    """Explicit candidate tab selection via server-generated opaque tab_id only."""

    model_config = ConfigDict(extra="forbid")

    tab_id: str = Field(min_length=4, max_length=120)


class AnswerQuestionItem(BaseModel):
    """Step-scoped answer submission for a single answerable missing_answer blocker."""

    model_config = ConfigDict(extra="forbid")

    field_id: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=4000)
    save_to_profile: bool = False


class AnswerQuestionsRequest(BaseModel):
    """Submit answers for answerable blockers on the active inspected step."""

    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(min_length=4, max_length=120)
    answers: list[AnswerQuestionItem] = Field(min_length=1, max_length=40)
    profile_revision: int | None = Field(default=None, ge=0)
