"""Master Profile Pydantic v2 schemas and LLM-safe projection helpers."""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from backend.app.core.time import normalize_nfc, parse_utc_rfc3339, utf16_len

MAX_IMPORT_TEXT_CHARS = 60_000
MAX_EXPERIENCE_ITEMS = 60
MAX_EDUCATION_ITEMS = 30
MAX_PROJECT_ITEMS = 40
MAX_SKILL_ITEMS = 250
MAX_LANGUAGE_ITEMS = 25
MAX_STANDARD_ANSWER_ITEMS = 150
MAX_IMPORT_ITEMS = 100
MAX_SUMMARY_ITEMS = 60

_GOV_ID_KEY_RE = re.compile(r"\b(cpf|rg|cnh|passaporte|passport|ssn|pis|titulo_eleitor|tax_id)\b", re.I)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u202a-\u202e\u2066-\u2069]")
_HTML_TAG_RE = re.compile(r"<[A-Za-z!/][^>]*>")
_ALLOWED_SLOTS = frozenset({"company_name", "role_title", "tech_stack_highlight"})
_SLOT_RE = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")


def new_item_id(prefix: str = "item") -> str:
    """Generate a compact URL-safe unique item ID."""
    import uuid

    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _validate_nonempty_clean(text: str) -> str:
    if _CONTROL_CHAR_RE.search(text):
        raise ValueError("Texto contém caracteres de controle não permitidos.")
    normalized = normalize_nfc(text)
    if not normalized.strip():
        raise ValueError("Campo de texto obrigatório não pode ficar em branco.")
    return normalized


def _validate_token(text: str) -> str:
    clean = _validate_nonempty_clean(text).strip()
    if not clean:
        raise ValueError("Token não pode ser vazio.")
    return clean


def _validate_optional_tz_dt(value: datetime) -> datetime:
    return parse_utc_rfc3339(value)


def _validate_variant_text(text: str, *, max_utf16: int, single_line: bool = False) -> str:
    if _CONTROL_CHAR_RE.search(text):
        raise ValueError("Texto contém caracteres de controle não permitidos.")
    if _HTML_TAG_RE.search(text):
        raise ValueError("Marcação HTML não é permitida em variantes de texto.")
    normalized = normalize_nfc(text)
    if not normalized.strip():
        raise ValueError("Texto da variante não pode ficar vazio.")
    if single_line and ("\n" in normalized or "\r" in normalized):
        raise ValueError("Variante curta deve estar em linha única.")
    if utf16_len(normalized) > max_utf16:
        raise ValueError(f"Texto excede o limite de {max_utf16} unidades UTF-16.")
    _validate_slot_placeholders(normalized)
    return normalized


def _validate_slot_placeholders(text: str) -> list[Literal["company_name", "role_title", "tech_stack_highlight"]]:
    """Validate template placeholders and return the ordered declared slots present."""
    used: list[Literal["company_name", "role_title", "tech_stack_highlight"]] = []
    # Strip valid slots first, then ensure no stray braces remain.
    remainder = text
    for match in _SLOT_RE.finditer(text):
        slot_name = match.group(1)
        if slot_name not in _ALLOWED_SLOTS:
            raise ValueError(f"Placeholder desconhecido ou inválido: '{{{{{slot_name}}}}}'")
        if slot_name == "company_name" and "company_name" not in used:
            used.append("company_name")
        elif slot_name == "role_title" and "role_title" not in used:
            used.append("role_title")
        elif slot_name == "tech_stack_highlight" and "tech_stack_highlight" not in used:
            used.append("tech_stack_highlight")
    remainder = _SLOT_RE.sub("", remainder)
    if "{{" in remainder or "}}" in remainder or "{" in remainder or "}" in remainder:
        raise ValueError("Chave de placeholder malformada no texto.")
    return used


NonEmptyId = Annotated[StrictStr, Field(min_length=1, max_length=64), AfterValidator(_validate_token)]
BoundedToken = Annotated[StrictStr, Field(min_length=1, max_length=120), AfterValidator(_validate_token)]
SignatureToken = Annotated[StrictStr, Field(min_length=1, max_length=240), AfterValidator(_validate_token)]
HexSha256 = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
StrictRevision = Annotated[StrictInt, Field(ge=1)]
StrictNonNegInt = Annotated[StrictInt, Field(ge=0)]
UtcDateTime = Annotated[datetime, AfterValidator(_validate_optional_tz_dt)]

Locale = Literal["pt-BR", "en"]
RoleTag = Literal[
    "data_engineering",
    "analytics_engineering",
    "backend_python",
    "ml_ai",
    "intern_junior",
    "general",
]
Archetype = Literal["all", "data_engineer", "backend_python", "ml_engineer", "intern_junior"]
Seniority = Literal["intern", "junior", "mid", "senior", "staff", "lead", "unknown"]
Contract = Literal["CLT", "PJ", "Estágio", "full_time", "part_time", "other", "unknown"]
Currency = Literal["BRL", "USD"]
Period = Literal["hourly", "monthly", "annual"]


class StrictModel(BaseModel):
    """Strict schema base preserving intentional paragraph whitespace."""

    model_config = ConfigDict(extra="forbid")


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


# ============================================================================
# Section 2 Master Profile V2, Routing, Arsenal, Pre-Flight, Actions & Tuning
# ============================================================================


class RouteClause(StrictModel):
    archetype_in: list[Archetype] = Field(default_factory=list, max_length=32)
    seniority_in: list[Seniority] = Field(default_factory=list, max_length=32)
    contract_in: list[Contract] = Field(default_factory=list, max_length=32)
    locales: list[Locale] = Field(default_factory=list, max_length=32)
    all_skills: list[BoundedToken] = Field(default_factory=list, max_length=32)
    any_skills: list[BoundedToken] = Field(default_factory=list, max_length=32)
    all_keywords: list[BoundedToken] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def _reject_all_empty_clause(self) -> RouteClause:
        if not (
            self.archetype_in
            or self.seniority_in
            or self.contract_in
            or self.locales
            or self.all_skills
            or self.any_skills
            or self.all_keywords
        ):
            raise ValueError("RouteClause deve especificar ao menos um critério não vazio.")
        return self


class Routing(StrictModel):
    role_tags: list[RoleTag] = Field(default_factory=list, max_length=16)
    when_to_use: list[RouteClause] = Field(default_factory=list, max_length=16)
    when_not_to_use: list[RouteClause] = Field(default_factory=list, max_length=16)


class VettedText(Routing):
    id: NonEmptyId
    language: Locale
    text: StrictStr = Field(min_length=1, max_length=1200)
    source_claim_ids: list[NonEmptyId] = Field(max_length=32)
    status: Literal["draft", "approved"] = "draft"

    @field_validator("text")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        clean = _validate_nonempty_clean(value)
        if utf16_len(clean) > 600:
            raise ValueError("Texto de variante excede o limite de 600 unidades UTF-16.")
        return clean


class RoutedBullet(ExperienceBullet):
    role_tags: list[RoleTag] = Field(default_factory=list, max_length=16)
    when_to_use: list[RouteClause] = Field(default_factory=list, max_length=16)
    when_not_to_use: list[RouteClause] = Field(default_factory=list, max_length=16)
    language: Locale | None = None
    evidence: list[EvidenceRef] = Field(default_factory=list, max_length=10)
    variants: list[VettedText] = Field(default_factory=list, max_length=12)


class RoutedExperienceEntry(ExperienceEntry):
    role_tags: list[RoleTag] = Field(default_factory=list, max_length=16)
    when_to_use: list[RouteClause] = Field(default_factory=list, max_length=16)
    when_not_to_use: list[RouteClause] = Field(default_factory=list, max_length=16)
    bullets: list[RoutedBullet] = Field(default_factory=list, max_length=20)  # type: ignore[assignment]


class RoutedProjectEntry(ProjectEntry):
    role_tags: list[RoleTag] = Field(default_factory=list, max_length=16)
    when_to_use: list[RouteClause] = Field(default_factory=list, max_length=16)
    when_not_to_use: list[RouteClause] = Field(default_factory=list, max_length=16)
    bullets: list[RoutedBullet] = Field(default_factory=list, max_length=15)  # type: ignore[assignment]


class RoutedStandardAnswerEntry(StandardAnswerEntry):
    role_tags: list[RoleTag] = Field(default_factory=list, max_length=16)
    when_to_use: list[RouteClause] = Field(default_factory=list, max_length=16)
    when_not_to_use: list[RouteClause] = Field(default_factory=list, max_length=16)
    language: Locale | None = None
    source_claim_ids: list[NonEmptyId] = Field(default_factory=list, max_length=32)


class MasterProfileDocumentV2(MasterProfileDocument):
    schema_version: Literal[2] = 2
    experience: list[RoutedExperienceEntry] = Field(  # type: ignore[assignment]
        default_factory=list, max_length=MAX_EXPERIENCE_ITEMS
    )
    projects: list[RoutedProjectEntry] = Field(  # type: ignore[assignment]
        default_factory=list, max_length=MAX_PROJECT_ITEMS
    )
    standard_answers: list[RoutedStandardAnswerEntry] = Field(  # type: ignore[assignment]
        default_factory=list, max_length=MAX_STANDARD_ANSWER_ITEMS
    )
    summaries: list[VettedText] = Field(default_factory=list, max_length=MAX_SUMMARY_ITEMS)

    @model_validator(mode="before")
    @classmethod
    def _upgrade_v1_schema_version(cls, data: Any) -> Any:
        if isinstance(data, BaseModel):
            data = data.model_dump(mode="python")
        if isinstance(data, dict):
            raw_ver = data.get("schema_version")
            if raw_ver is None or raw_ver == 1:
                patched = dict(data)
                patched["schema_version"] = 2
                return patched
        return data

    @model_validator(mode="after")
    def _ensure_v2_claim_integrity(self) -> MasterProfileDocumentV2:
        global_ids: set[str] = set()
        canonical_factual_ids: set[str] = set()

        def _claim(item_id: str, *, is_canonical_fact: bool = True) -> None:
            if item_id in global_ids:
                raise ValueError(f"Duplicate profile entry id: {item_id}")
            global_ids.add(item_id)
            if is_canonical_fact:
                canonical_factual_ids.add(item_id)

        for exp in self.experience:
            _claim(exp.id)
            for b in exp.bullets:
                _claim(b.id)
                for v in b.variants:
                    _claim(v.id, is_canonical_fact=False)
        for edu in self.education:
            _claim(edu.id)
        for proj in self.projects:
            _claim(proj.id)
            for pb in proj.bullets:
                _claim(pb.id)
                for v in pb.variants:
                    _claim(v.id, is_canonical_fact=False)
        for skill in self.skills:
            _claim(skill.id)
        for lang in self.languages:
            _claim(lang.id)
        for imp in self.imports:
            _claim(imp.id)
        for ans in self.standard_answers:
            _claim(ans.id, is_canonical_fact=False)
        for summary in self.summaries:
            if utf16_len(summary.text) > 500:
                raise ValueError("Texto de resumo profissional excede 500 unidades UTF-16.")
            _claim(summary.id, is_canonical_fact=False)

        # Verify source_claim_ids reference only canonical factual items (never generated variants/summaries)
        for exp in self.experience:
            for b in exp.bullets:
                for v in b.variants:
                    for cid in v.source_claim_ids:
                        if cid not in canonical_factual_ids:
                            raise ValueError(f"Referência source_claim_ids inválida em variante '{v.id}': {cid}")
        for proj in self.projects:
            for pb in proj.bullets:
                for v in pb.variants:
                    for cid in v.source_claim_ids:
                        if cid not in canonical_factual_ids:
                            raise ValueError(f"Referência source_claim_ids inválida em variante '{v.id}': {cid}")
        for summary in self.summaries:
            for cid in summary.source_claim_ids:
                if cid not in canonical_factual_ids:
                    raise ValueError(f"Referência source_claim_ids inválida em resumo '{summary.id}': {cid}")
        for ans in self.standard_answers:
            for cid in ans.source_claim_ids:
                if cid not in canonical_factual_ids:
                    raise ValueError(f"Referência source_claim_ids inválida em resposta '{ans.id}': {cid}")
        return self


def _normalize_nested_nfc(value: Any) -> Any:
    """Recursively NFC-normalize strings in JSON-like dicts/lists while retaining paragraph whitespace."""
    if isinstance(value, str):
        cleaned = _CONTROL_CHAR_RE.sub("", value)
        return normalize_nfc(cleaned)
    if isinstance(value, list):
        return [_normalize_nested_nfc(v) for v in value]
    if isinstance(value, dict):
        return {k: _normalize_nested_nfc(v) for k, v in value.items()}
    return value


def upgrade_profile_document_v1_to_v2(raw_doc: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Upgrade a v1 or unversioned profile dict into a validated canonical v2 dictionary."""
    if not isinstance(raw_doc, dict):
        raise ValueError("Documento MasterProfile deve ser um objeto JSON.")
    raw_version = raw_doc.get("schema_version")
    normalized = _normalize_nested_nfc(raw_doc)
    doc = MasterProfileDocumentV2.model_validate(normalized)
    upgraded_json = doc.model_dump(mode="json")
    from_ver = (
        1
        if raw_version is None
        else (raw_version if isinstance(raw_version, int) and not isinstance(raw_version, bool) else 2)
    )
    diagnostics = {
        "from_version": from_ver,
        "to_version": 2,
        "changed": upgraded_json != raw_doc,
        "counts": {
            "experience": len(doc.experience),
            "education": len(doc.education),
            "projects": len(doc.projects),
            "skills": len(doc.skills),
            "languages": len(doc.languages),
            "standard_answers": len(doc.standard_answers),
            "imports": len(doc.imports),
            "summaries": len(doc.summaries),
        },
    }
    return upgraded_json, diagnostics


class LengthVariants(StrictModel):
    short: StrictStr | None = None
    medium: StrictStr | None = None
    long: StrictStr | None = None

    @field_validator("short")
    @classmethod
    def _check_short(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_variant_text(value, max_utf16=150, single_line=True)

    @field_validator("medium")
    @classmethod
    def _check_medium(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_variant_text(value, max_utf16=500)

    @field_validator("long")
    @classmethod
    def _check_long(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_variant_text(value, max_utf16=1500)

    @model_validator(mode="after")
    def _require_nonempty_variant(self) -> LengthVariants:
        if self.short is None and self.medium is None and self.long is None:
            raise ValueError("LengthVariants requer pelo menos uma variação preenchida (short, medium ou long).")
        return self


ArsenalCategory = Literal[
    "professional_summary",
    "why_company",
    "technical_challenge",
    "finops_cloud_impact",
    "availability",
    "work_authorization",
    "english_proficiency",
    "salary_note",
    "custom",
]
ArsenalSlot = Literal["company_name", "role_title", "tech_stack_highlight"]
ArsenalProvenance = Literal["seed", "user", "legacy", "cloud"]


class ArsenalPayload(Routing):
    length_variants: LengthVariants
    trigger_keywords: list[BoundedToken] = Field(max_length=32)
    question_signatures: list[SignatureToken] = Field(default_factory=list, max_length=32)
    slots: list[ArsenalSlot] = Field(default_factory=list, max_length=3)
    source_claim_ids: list[NonEmptyId] = Field(default_factory=list, max_length=32)
    provenance: ArsenalProvenance

    @model_validator(mode="after")
    def _validate_slots_coverage(self) -> ArsenalPayload:
        used: set[ArsenalSlot] = set()
        for variant in (
            self.length_variants.short,
            self.length_variants.medium,
            self.length_variants.long,
        ):
            if variant:
                used.update(_validate_slot_placeholders(variant))
        missing_decl = used - set(self.slots)
        if missing_decl:
            raise ValueError(
                f"Placeholders usados precisam constar em slots: {', '.join(sorted(missing_decl))}"
            )
        return self


class BaseAnswerArsenal(Routing):
    id: NonEmptyId
    revision: StrictRevision
    status: Literal["draft", "approved", "retired"]
    category: ArsenalCategory
    language: Locale
    role_archetype: Archetype
    length_variants: LengthVariants
    trigger_keywords: list[BoundedToken] = Field(max_length=32)
    question_signatures: list[SignatureToken] = Field(default_factory=list, max_length=32)
    slots: list[ArsenalSlot] = Field(default_factory=list, max_length=3)
    source_claim_ids: list[NonEmptyId] = Field(default_factory=list, max_length=32)
    provenance: ArsenalProvenance
    approved_at: UtcDateTime | None = None

    @model_validator(mode="after")
    def _validate_arsenal_state(self) -> BaseAnswerArsenal:
        if self.status == "approved" and self.approved_at is None:
            raise ValueError("Entrada aprovada do Arsenal exige approved_at definido.")
        used: set[ArsenalSlot] = set()
        for variant in (
            self.length_variants.short,
            self.length_variants.medium,
            self.length_variants.long,
        ):
            if variant:
                used.update(_validate_slot_placeholders(variant))
        missing_decl = used - set(self.slots)
        if missing_decl:
            raise ValueError(
                f"Placeholders usados precisam constar em slots: {', '.join(sorted(missing_decl))}"
            )
        return self


class FieldOption(StrictModel):
    key: SignatureToken
    label: BoundedToken


FieldControlType = Literal[
    "text",
    "textarea",
    "tel",
    "email",
    "url",
    "number",
    "date",
    "select",
    "radio",
    "checkbox",
    "file",
    "unsupported",
]


class FieldSnapshot(StrictModel):
    label: BoundedToken
    control: FieldControlType
    locale: Locale | None = None
    required: StrictBool
    min_chars: StrictNonNegInt | None = None
    max_chars: StrictNonNegInt | None = None
    max_words: StrictNonNegInt | None = None
    pattern: SignatureToken | None = None
    minimum: BoundedToken | None = None
    maximum: BoundedToken | None = None
    step: BoundedToken | None = None
    options: list[FieldOption] = Field(default_factory=list, max_length=200)
    salary_contract: Contract | None = None
    salary_currency: Currency | None = None
    salary_period: Period | None = None
    salary_value_shape: Literal["amount", "range", "option", "ambiguous"] | None = None

    @model_validator(mode="after")
    def _validate_char_bounds(self) -> FieldSnapshot:
        if self.min_chars is not None and self.max_chars is not None and self.min_chars > self.max_chars:
            raise ValueError("min_chars não pode exceder max_chars.")
        return self


class SalaryBand(StrictModel):
    id: NonEmptyId
    role_archetype: Archetype
    seniority: Seniority
    contract: Contract
    currency: Currency
    period: Period
    min_minor: StrictNonNegInt
    max_minor: StrictNonNegInt
    basis: BoundedToken
    source_note: SignatureToken
    as_of: date

    @model_validator(mode="after")
    def _validate_band_bounds(self) -> SalaryBand:
        if self.min_minor > self.max_minor:
            raise ValueError("min_minor não pode ser maior que max_minor em SalaryBand.")
        return self


class SalarySuggestion(StrictModel):
    contract: Contract
    seniority: Seniority
    currency: Currency | None = None
    period: Period | None = None
    min_minor: StrictNonNegInt | None = None
    max_minor: StrictNonNegInt | None = None
    band_id: NonEmptyId | None = None
    basis: BoundedToken | None = None
    source_note: SignatureToken | None = None
    as_of: date | None = None
    reason_code: Literal["configured_band", "missing_band", "unknown_context", "stale_band"]

    @model_validator(mode="after")
    def _validate_suggestion_shape(self) -> SalarySuggestion:
        band_fields = (
            self.currency,
            self.period,
            self.min_minor,
            self.max_minor,
            self.band_id,
            self.basis,
            self.source_note,
            self.as_of,
        )
        if self.reason_code == "configured_band":
            if any(v is None for v in band_fields):
                raise ValueError("SalarySuggestion com configured_band exige faixa e metadados completos.")
            assert self.min_minor is not None and self.max_minor is not None
            if self.min_minor > self.max_minor:
                raise ValueError("min_minor não pode exceder max_minor em SalarySuggestion.")
        else:
            if any(
                v is not None
                for v in (self.min_minor, self.max_minor, self.band_id, self.basis, self.source_note, self.as_of)
            ):
                raise ValueError(
                    "SalarySuggestion sem faixa utilizável não deve conter valores ou metadados de faixa."
                )
        return self


class SalaryRangeValue(StrictModel):
    min_minor: StrictNonNegInt
    max_minor: StrictNonNegInt

    @model_validator(mode="after")
    def _validate_range(self) -> SalaryRangeValue:
        if self.min_minor > self.max_minor:
            raise ValueError("min_minor não pode exceder max_minor em SalaryRangeValue.")
        return self


class SalaryApproval(StrictModel):
    kind: Literal["salary"]
    shape: Literal["amount", "range", "option"]
    value: StrictNonNegInt | SalaryRangeValue | BoundedToken
    currency: Currency
    period: Period

    @model_validator(mode="after")
    def _validate_shape_value(self) -> SalaryApproval:
        if self.shape == "amount":
            if isinstance(self.value, bool) or not isinstance(self.value, int) or self.value < 0:
                raise ValueError("SalaryApproval com shape='amount' exige inteiro não negativo em minor units.")
        elif self.shape == "range" and not isinstance(self.value, SalaryRangeValue):
            raise ValueError("SalaryApproval com shape='range' exige objeto SalaryRangeValue.")
        elif self.shape == "option" and (not isinstance(self.value, str) or not self.value.strip()):
            raise ValueError("SalaryApproval com shape='option' exige chave exata da opção.")
        return self


class FieldApproval(StrictModel):
    kind: Literal["text", "number", "option", "boolean"]
    value: StrictStr | StrictBool

    @model_validator(mode="after")
    def _validate_field_value(self) -> FieldApproval:
        if self.kind == "boolean":
            if not isinstance(self.value, bool):
                raise ValueError("FieldApproval kind='boolean' exige booleano estrito.")
        else:
            if not isinstance(self.value, str):
                raise ValueError(f"FieldApproval kind='{self.kind}' exige string.")
            if _CONTROL_CHAR_RE.search(self.value):
                raise ValueError("Valor contém caracteres de controle não permitidos.")
        return self


ApprovalValue = Annotated[SalaryApproval | FieldApproval, Field(discriminator="kind")]


class PendingManualAction(StrictModel):
    id: NonEmptyId
    application_id: NonEmptyId
    step_key: BoundedToken
    field_key: BoundedToken
    field_fingerprint: HexSha256
    kind: Literal["salary", "sensitive", "ambiguous", "low_confidence", "invalid", "manual_step"]
    status: Literal["pending", "approved", "applied", "skipped", "stale"]
    field: FieldSnapshot
    suggestion: SalarySuggestion | None = None
    revision: StrictRevision
    created_at: UtcDateTime
    updated_at: UtcDateTime
    approved_at: UtcDateTime | None = None
    applied_at: UtcDateTime | None = None

    @model_validator(mode="after")
    def _validate_action_invariants(self) -> PendingManualAction:
        if self.status == "approved" and self.approved_at is None:
            raise ValueError("Ação aprovada requer approved_at.")
        if self.status == "applied" and (self.approved_at is None or self.applied_at is None):
            raise ValueError("Ação aplicada requer approved_at e applied_at.")
        if self.applied_at is not None and self.status not in {"applied", "stale"}:
            raise ValueError("applied_at só é permitido nos estados applied ou stale.")
        if self.kind != "salary" and self.suggestion is not None:
            raise ValueError("suggestion é permitida apenas para ações do tipo salary.")
        return self


class PortalSessionStatus(StrictModel):
    portal_key: BoundedToken
    scope_key: BoundedToken
    status: Literal[
        "unknown",
        "checking",
        "authenticated",
        "login_required",
        "public_access",
        "challenge",
        "unavailable",
        "disconnected",
    ]
    reason_code: BoundedToken | None = None
    checked_at: UtcDateTime | None = None
    expires_at: UtcDateTime | None = None
    revision: StrictRevision

    @model_validator(mode="after")
    def _validate_session_timestamps(self) -> PortalSessionStatus:
        if self.status in {"authenticated", "public_access", "login_required", "challenge"}:
            if self.checked_at is None or self.expires_at is None:
                raise ValueError(f"Status '{self.status}' exige checked_at e expires_at.")
            if self.expires_at <= self.checked_at:
                raise ValueError("expires_at deve ser posterior a checked_at.")
        return self


class FeedbackEvidence(StrictModel):
    archetype: Archetype
    seniority: Seniority
    skill_keys: list[BoundedToken] = Field(default_factory=list, max_length=64)
    company_key: BoundedToken
    workplace: Literal["remote", "hybrid", "onsite", "unknown"]
    normalized_city: BoundedToken | None = None


class JobDiscoveryFeedback(StrictModel):
    id: NonEmptyId
    job_id: NonEmptyId
    action: Literal[
        "high_fit",
        "too_senior",
        "wrong_tech_stack",
        "onsite_outside_recife",
        "blacklist_company",
    ]
    operation: Literal["apply", "undo"]
    evidence: FeedbackEvidence
    created_at: UtcDateTime


class TuningRuleSet(StrictModel):
    version: Literal[1] = 1
    keyword_weights: dict[BoundedToken, StrictInt] = Field(default_factory=dict)
    seniority_weights: dict[Archetype, dict[Seniority, StrictInt]] = Field(default_factory=dict)
    negative_keywords: list[BoundedToken] = Field(default_factory=list)
    blocked_company_keys: list[BoundedToken] = Field(default_factory=list)
    location_policy: Literal["remote_eligible_or_recife"] = "remote_eligible_or_recife"


# ============================================================================
# Phase 1 Import Outcome & Repair Preview Contracts
# ============================================================================


class ImportWarning(StrictModel):
    code: BoundedToken
    message: StrictStr = Field(min_length=1, max_length=500)
    section: BoundedToken | None = None
    line: StrictRevision | None = None
    page: StrictRevision | None = None


class ImportSectionCounts(StrictModel):
    experience: StrictNonNegInt = 0
    education: StrictNonNegInt = 0
    projects: StrictNonNegInt = 0
    skills: StrictNonNegInt = 0
    languages: StrictNonNegInt = 0
    bullets: StrictNonNegInt = 0
    excluded_blocks: StrictNonNegInt = 0


class ImportResult(StrictModel):
    document_id: NonEmptyId
    duplicate: StrictBool
    counts: ImportSectionCounts
    warnings: list[ImportWarning] = Field(default_factory=list)


class ProfileImportSummaryLegacy(StrictModel):
    filename: BoundedToken
    sha256: HexSha256
    deduplicated: StrictBool
    added_skills: StrictNonNegInt = 0
    added_experience: StrictNonNegInt = 0
    added_education: StrictNonNegInt = 0
    added_projects: StrictNonNegInt = 0
    added_languages: StrictNonNegInt = 0


class ProfileGetResponse(StrictModel):
    document: MasterProfileDocumentV2
    revision: StrictNonNegInt
    updated_at: StrictStr | None = None


class ProfileImportResponse(ProfileGetResponse):
    import_summary: ProfileImportSummaryLegacy
    import_result: ImportResult


class ProfilePutRequest(StrictModel):
    document: MasterProfileDocumentV2
    revision: StrictNonNegInt


class ImportRepairPreviewRequest(StrictModel):
    base_revision: StrictNonNegInt


class ImportRepairDiff(StrictModel):
    grouped_replacements: list[dict[str, Any]] = Field(default_factory=list)
    orphaned_content: list[dict[str, Any]] = Field(default_factory=list)
    recovered_education: list[dict[str, Any]] = Field(default_factory=list)
    removed_empty_skills: list[str] = Field(default_factory=list)
    preserved_user_edited_ids: list[NonEmptyId] = Field(default_factory=list)
    id_mapping: dict[NonEmptyId, NonEmptyId] = Field(default_factory=dict)
    counts: ImportSectionCounts = Field(default_factory=ImportSectionCounts)


class ImportRepairPreviewResponse(StrictModel):
    document_id: NonEmptyId
    source_sha256: HexSha256
    base_revision: StrictNonNegInt
    proposed_document: MasterProfileDocumentV2
    diff: ImportRepairDiff
    warnings: list[ImportWarning] = Field(default_factory=list)
