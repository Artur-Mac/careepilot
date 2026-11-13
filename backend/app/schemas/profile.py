"""Master Profile Pydantic v2 schemas and LLM-safe projection helpers."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_IMPORT_TEXT_CHARS = 60_000
MAX_EXPERIENCE_ITEMS = 60
MAX_EDUCATION_ITEMS = 30
MAX_PROJECT_ITEMS = 40
MAX_SKILL_ITEMS = 250
MAX_LANGUAGE_ITEMS = 25
MAX_STANDARD_ANSWER_ITEMS = 150
MAX_IMPORT_ITEMS = 100

_GOV_ID_KEY_RE = re.compile(r"\b(cpf|rg|cnh|passaporte|passport|ssn|pis|titulo_eleitor|tax_id)\b", re.I)


def new_item_id(prefix: str = "item") -> str:
    """Generate a compact URL-safe unique item ID."""
    import uuid

    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ContactLinks(_StrictModel):
    linkedin: str | None = None
    github: str | None = None
    portfolio: str | None = None
    other: list[str] = Field(default_factory=list, max_length=12)


class ContactInfo(_StrictModel):
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None
    links: ContactLinks = Field(default_factory=ContactLinks)


class EvidenceRef(_StrictModel):
    document_name: str | None = None
    section_or_page: str | None = None
    snippet: str | None = None
    source_sha256: str | None = None


class ExperienceBullet(_StrictModel):
    id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=600)


class ExperienceEntry(_StrictModel):
    id: str = Field(min_length=1, max_length=64)
    company: str | None = None
    role: str | None = None
    period: str | None = None
    location: str | None = None
    details: str = ""
    bullets: list[ExperienceBullet] = Field(default_factory=list, max_length=20)
    source: str | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=10)


class EducationEntry(_StrictModel):
    id: str = Field(min_length=1, max_length=64)
    institution: str | None = None
    degree: str | None = None
    period: str | None = None
    details: str = ""
    source: str | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=10)


class ProjectEntry(_StrictModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=240)
    url: str | None = None
    details: str = ""
    bullets: list[ExperienceBullet] = Field(default_factory=list, max_length=15)
    source: str | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=10)


class SkillEntry(_StrictModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    category: str | None = None
    level: str | None = None
    source: str | None = None


class LanguageEntry(_StrictModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    proficiency: str | None = None
    details: str | None = None
    source: str | None = None


class PreferencesInfo(_StrictModel):
    target_roles: list[str] = Field(default_factory=list, max_length=20)
    location: str | None = None
    work_model: list[str] = Field(default_factory=list, max_length=8)
    salary_amount: float | None = Field(default=None, ge=0.0)
    salary_currency: str | None = Field(default=None, max_length=16)
    salary_period: Literal["hourly", "monthly", "annual"] | None = None
    salary_text: str | None = Field(default=None, max_length=160)
    availability: str | None = Field(default=None, max_length=120)
    work_authorization: str | None = Field(default=None, max_length=160)
    seniority: list[str] = Field(default_factory=list, max_length=10)
    contract_types: list[str] = Field(default_factory=list, max_length=10)
    excluded_companies: list[str] = Field(default_factory=list, max_length=40)


class StandardAnswerEntry(_StrictModel):
    id: str = Field(min_length=1, max_length=64)
    question_signature: str | None = Field(default=None, max_length=240)
    label: str = Field(min_length=1, max_length=400)
    answer: str = Field(max_length=4000)
    kind: Literal["text", "option", "number", "boolean"] = "text"
    source: str | None = None
    updated_at: str | None = None


class ImportedDocumentEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    sha256: str = Field(min_length=16, max_length=64)
    filename: str = Field(min_length=1, max_length=256)
    extracted_text: str = Field(min_length=1, max_length=MAX_IMPORT_TEXT_CHARS)
    kind: Literal["resume_import", "migrated_note"] = "resume_import"
    source: str | None = None
    imported_at: str | None = None


class MasterProfileDocument(_StrictModel):
    contact: ContactInfo = Field(default_factory=ContactInfo)
    experience: list[ExperienceEntry] = Field(default_factory=list, max_length=MAX_EXPERIENCE_ITEMS)
    education: list[EducationEntry] = Field(default_factory=list, max_length=MAX_EDUCATION_ITEMS)
    projects: list[ProjectEntry] = Field(default_factory=list, max_length=MAX_PROJECT_ITEMS)
    skills: list[SkillEntry] = Field(default_factory=list, max_length=MAX_SKILL_ITEMS)
    languages: list[LanguageEntry] = Field(default_factory=list, max_length=MAX_LANGUAGE_ITEMS)
    preferences: PreferencesInfo = Field(default_factory=PreferencesInfo)
    standard_answers: list[StandardAnswerEntry] = Field(
        default_factory=list, max_length=MAX_STANDARD_ANSWER_ITEMS
    )
    imports: list[ImportedDocumentEntry] = Field(default_factory=list, max_length=MAX_IMPORT_ITEMS)

    @model_validator(mode="after")
    def _ensure_unique_claim_ids(self) -> MasterProfileDocument:
        seen: set[str] = set()
        for group in (
            self.experience,
            self.education,
            self.projects,
            self.skills,
            self.languages,
            self.standard_answers,
            self.imports,
        ):
            for entry in group:
                if entry.id in seen:
                    raise ValueError(f"Duplicate profile entry id: {entry.id}")
                seen.add(entry.id)
        for exp in self.experience:
            bullet_seen: set[str] = set()
            for bullet in exp.bullets:
                if bullet.id in bullet_seen or bullet.id in seen:
                    raise ValueError(f"Duplicate experience bullet id: {bullet.id}")
                bullet_seen.add(bullet.id)
        for proj in self.projects:
            bullet_seen = set()
            for bullet in proj.bullets:
                if bullet.id in bullet_seen or bullet.id in seen:
                    raise ValueError(f"Duplicate project bullet id: {bullet.id}")
                bullet_seen.add(bullet.id)
        return self

    def has_resume_content(self) -> bool:
        """Return True when the profile holds enough real candidate claims to build a PDF."""
        has_name = bool((self.contact.name or "").strip())
        has_claims = bool(
            self.experience or self.education or self.projects or self.skills or self.imports
        )
        return has_name and has_claims

    def to_llm_payload(self, *, max_import_excerpt_chars: int = 4000) -> dict[str, Any]:
        """Project candidate profile into an LLM-safe dict without credentials or govt IDs."""
        safe_location = ", ".join(
            part for part in (self.contact.city, self.contact.state, self.contact.country) if part
        )
        return {
            "candidate_header": {
                "name": self.contact.name,
                "location": safe_location or self.preferences.location,
                "links": self.contact.links.model_dump(exclude_none=True),
            },
            "preferences": {
                "target_roles": self.preferences.target_roles,
                "work_model": self.preferences.work_model,
                "seniority": self.preferences.seniority,
                "availability": self.preferences.availability,
                "work_authorization": self.preferences.work_authorization,
            },
            "skills": [{"id": s.id, "name": s.name, "level": s.level} for s in self.skills],
            "languages": [
                {"id": lang.id, "name": lang.name, "proficiency": lang.proficiency}
                for lang in self.languages
            ],
            "experience": [
                {
                    "id": exp.id,
                    "company": exp.company,
                    "role": exp.role,
                    "period": exp.period,
                    "details": exp.details,
                    "bullets": [{"id": b.id, "text": b.text} for b in exp.bullets],
                }
                for exp in self.experience
            ],
            "education": [
                {
                    "id": edu.id,
                    "institution": edu.institution,
                    "degree": edu.degree,
                    "period": edu.period,
                    "details": edu.details,
                }
                for edu in self.education
            ],
            "projects": [
                {
                    "id": proj.id,
                    "name": proj.name,
                    "url": proj.url,
                    "details": proj.details,
                    "bullets": [{"id": b.id, "text": b.text} for b in proj.bullets],
                }
                for proj in self.projects
            ],
            "imports": [
                {
                    "id": imp.id,
                    "filename": imp.filename,
                    "kind": imp.kind,
                    "text_length": len(imp.extracted_text),
                    "text": _strip_sensitive_gov_lines(imp.extracted_text[:max_import_excerpt_chars]),
                }
                for imp in self.imports
                if imp.kind == "resume_import"
            ],
        }


def _strip_sensitive_gov_lines(text: str) -> str:
    kept: list[str] = []
    for line in text.splitlines():
        if _GOV_ID_KEY_RE.search(line):
            continue
        kept.append(line)
    return "\n".join(kept)


def make_note_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ProfileGetResponse(BaseModel):
    document: MasterProfileDocument
    revision: int
    updated_at: str | None = None


class ProfilePutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document: MasterProfileDocument
    revision: int = Field(ge=0)
