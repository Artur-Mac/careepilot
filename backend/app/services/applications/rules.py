"""Ordered typed field-matching rules, intent classification, and canonical profile projection."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from backend.app.schemas.profile import ArsenalCategory, Locale, MasterProfileDocument

FieldIntent = Literal[
    "name",
    "email",
    "phone",
    "location",
    "url",
    "availability",
    "work_authorization",
    "english_proficiency",
    "salary",
    "summary",
    "motivation",
    "case",
    "unknown",
]

RuleSensitivity = Literal["public", "contact", "sensitive_pii", "regulated", "salary"]
RuleSource = Literal[
    "portal_blocker",
    "manual_only",
    "salary_queue",
    "pending_approval",
    "master_profile",
    "arsenal",
    "application_answer",
]

_ALL_TEXT_CONTROLS: tuple[str, ...] = ("text", "textarea")
_TEXTLIKE_CONTROLS: tuple[str, ...] = ("text", "textarea", "email", "tel", "url", "number", "date")


def normalize_rule_text(raw: str) -> str:
    """Fold accents and punctuation into lowercase single-space tokens."""
    folded = unicodedata.normalize("NFKD", raw or "")
    stripped = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", stripped.lower()).strip()


def _phrase_matches(norm_label: str, alias: str) -> bool:
    if not norm_label or not alias:
        return False
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", norm_label))


@dataclass(frozen=True, slots=True)
class FieldRule:
    """Immutable ordered rule governing field intent, sensitivity, source, and formatting."""

    rule_id: str
    priority: int
    intent: str
    semantic_intent: FieldIntent
    locales: tuple[Locale, ...]
    exact_aliases: tuple[str, ...]
    control_types: tuple[str, ...]
    sensitivity: RuleSensitivity
    source: RuleSource
    formatter: str | None
    requires_manual: bool
    profile_key: str | None = None
    arsenal_category: ArsenalCategory | None = None


ORDERED_FIELD_RULES: tuple[FieldRule, ...] = tuple(
    sorted(
        (
            # Priority 10-19: Regulated / manual-only safety controls (highest precedence)
            FieldRule(
                rule_id="rule-non-resume-file",
                priority=10,
                intent="unsupported_upload",
                semantic_intent="unknown",
                locales=("pt-BR", "en"),
                exact_aliases=(
                    "carta de apresentacao",
                    "cover letter",
                    "foto",
                    "photo",
                    "avatar",
                    "video",
                    "diploma",
                    "certificado",
                    "historico escolar",
                    "identidade",
                    "documento",
                ),
                control_types=("file",),
                sensitivity="regulated",
                source="portal_blocker",
                formatter=None,
                requires_manual=True,
            ),
            FieldRule(
                rule_id="rule-resume-file",
                priority=11,
                intent="resume_file",
                semantic_intent="unknown",
                locales=("pt-BR", "en"),
                exact_aliases=("curriculo", "curriculum", "cv", "resume"),
                control_types=("file",),
                sensitivity="public",
                source="master_profile",
                formatter="file_pdf",
                requires_manual=False,
            ),
            FieldRule(
                rule_id="rule-legal-consent",
                priority=12,
                intent="legal_consent",
                semantic_intent="unknown",
                locales=("pt-BR", "en"),
                exact_aliases=(
                    "concordo",
                    "aceito",
                    "declaro",
                    "li e aceito",
                    "politica de privacidade",
                    "privacy policy",
                    "termos de uso",
                    "terms of use",
                    "terms and conditions",
                    "lgpd",
                    "gdpr",
                    "marketing",
                    "receber comunicacoes",
                    "autorizo o tratamento",
                    "consentimento",
                    "attest",
                    "certify",
                    "acknowledge",
                    "declaration",
                ),
                control_types=("checkbox", "radio"),
                sensitivity="regulated",
                source="portal_blocker",
                formatter=None,
                requires_manual=True,
            ),
            FieldRule(
                rule_id="rule-credentials-challenge",
                priority=13,
                intent="credentials",
                semantic_intent="unknown",
                locales=("pt-BR", "en"),
                exact_aliases=(
                    "senha",
                    "password",
                    "codigo de verificacao",
                    "otp",
                    "2fa",
                    "token sms",
                    "captcha",
                ),
                control_types=("text", "number", "textarea"),
                sensitivity="regulated",
                source="manual_only",
                formatter=None,
                requires_manual=True,
            ),
            FieldRule(
                rule_id="rule-sensitive-demographic",
                priority=14,
                intent="sensitive_demographic",
                semantic_intent="unknown",
                locales=("pt-BR", "en"),
                exact_aliases=(
                    "raca",
                    "cor ou raca",
                    "etnia",
                    "ethnicity",
                    "race",
                    "genero",
                    "identidade de genero",
                    "gender",
                    "orientacao sexual",
                    "sexual orientation",
                    "pcd",
                    "pessoa com deficiencia",
                    "deficiencia",
                    "disability",
                    "veteran",
                    "veterano",
                    "self identify",
                    "autodeclaracao",
                ),
                control_types=("text", "textarea", "select", "radio", "checkbox"),
                sensitivity="sensitive_pii",
                source="manual_only",
                formatter=None,
                requires_manual=True,
            ),
            FieldRule(
                rule_id="rule-identity-proof",
                priority=15,
                intent="identity_proof",
                semantic_intent="unknown",
                locales=("pt-BR", "en"),
                exact_aliases=(
                    "rg",
                    "cnh",
                    "passaporte",
                    "passport",
                    "ssn",
                    "pis",
                    "pasep",
                    "titulo de eleitor",
                ),
                control_types=("text", "number", "file"),
                sensitivity="sensitive_pii",
                source="manual_only",
                formatter=None,
                requires_manual=True,
            ),
            # Priority 20: Salary Golden Rule (ALWAYS manual / approved queue only)
            FieldRule(
                rule_id="rule-salary",
                priority=20,
                intent="salary",
                semantic_intent="salary",
                locales=("pt-BR", "en"),
                exact_aliases=(
                    "pretensao salarial",
                    "salario",
                    "salarial",
                    "remuneracao",
                    "compensacao",
                    "salary",
                    "compensation",
                    "desired compensation",
                    "expected salary",
                    "expected pay",
                    "pay expectation",
                    "hourly rate",
                    "taxa horaria",
                    "valor mensal esperado",
                    "expectativa salarial",
                ),
                control_types=("text", "textarea", "number", "select", "radio"),
                sensitivity="salary",
                source="salary_queue",
                formatter="scalar_salary",
                requires_manual=True,
            ),
            # Priority 25-29: Sensitive PII requiring explicit current-app queue approval
            FieldRule(
                rule_id="rule-cpf",
                priority=25,
                intent="cpf",
                semantic_intent="unknown",
                locales=("pt-BR",),
                exact_aliases=("cpf", "cpf do candidato", "cadastro de pessoa fisica"),
                control_types=("text", "number"),
                sensitivity="sensitive_pii",
                source="pending_approval",
                formatter="cpf",
                requires_manual=True,
            ),
            FieldRule(
                rule_id="rule-cep",
                priority=26,
                intent="cep",
                semantic_intent="location",
                locales=("pt-BR", "en"),
                exact_aliases=("cep", "codigo postal", "postal code", "zip code"),
                control_types=("text", "number"),
                sensitivity="public",
                source="arsenal",
                formatter="cep",
                requires_manual=False,
                profile_key="contact.postal_code",
                arsenal_category="custom",
            ),
            FieldRule(
                rule_id="rule-birth-date",
                priority=27,
                intent="date",
                semantic_intent="unknown",
                locales=("pt-BR", "en"),
                exact_aliases=("data de nascimento", "birth date", "date of birth", "dob"),
                control_types=("date", "text"),
                sensitivity="sensitive_pii",
                source="pending_approval",
                formatter="date",
                requires_manual=True,
            ),
            # Priority 30-49: Canonical Master Profile scalar fields
            FieldRule(
                rule_id="rule-contact-email",
                priority=30,
                intent="email",
                semantic_intent="email",
                locales=("pt-BR", "en"),
                exact_aliases=("email", "e mail", "correo"),
                control_types=("email", "text"),
                sensitivity="contact",
                source="master_profile",
                formatter="email",
                requires_manual=False,
                profile_key="contact.email",
            ),
            FieldRule(
                rule_id="rule-contact-phone",
                priority=31,
                intent="phone",
                semantic_intent="phone",
                locales=("pt-BR", "en"),
                exact_aliases=("telefone", "celular", "whatsapp", "phone", "mobile", "fone"),
                control_types=("tel", "text"),
                sensitivity="contact",
                source="master_profile",
                formatter="phone",
                requires_manual=False,
                profile_key="contact.phone",
            ),
            FieldRule(
                rule_id="rule-contact-linkedin",
                priority=32,
                intent="linkedin",
                semantic_intent="url",
                locales=("pt-BR", "en"),
                exact_aliases=("linkedin", "perfil linkedin", "linkedin url"),
                control_types=("url", "text"),
                sensitivity="contact",
                source="master_profile",
                formatter="url",
                requires_manual=False,
                profile_key="contact.links.linkedin",
            ),
            FieldRule(
                rule_id="rule-contact-github",
                priority=33,
                intent="github",
                semantic_intent="url",
                locales=("pt-BR", "en"),
                exact_aliases=("github", "github url", "perfil github"),
                control_types=("url", "text"),
                sensitivity="contact",
                source="master_profile",
                formatter="url",
                requires_manual=False,
                profile_key="contact.links.github",
            ),
            FieldRule(
                rule_id="rule-contact-portfolio",
                priority=34,
                intent="portfolio",
                semantic_intent="url",
                locales=("pt-BR", "en"),
                exact_aliases=("portfolio", "portfolio pessoal", "site pessoal", "website", "personal site", "homepage"),
                control_types=("url", "text"),
                sensitivity="contact",
                source="master_profile",
                formatter="url",
                requires_manual=False,
                profile_key="contact.links.portfolio",
            ),
            FieldRule(
                rule_id="rule-contact-first-name",
                priority=35,
                intent="first_name",
                semantic_intent="name",
                locales=("pt-BR", "en"),
                exact_aliases=("primeiro nome", "first name", "given name"),
                control_types=("text",),
                sensitivity="contact",
                source="master_profile",
                formatter=None,
                requires_manual=False,
                profile_key="contact.first_name",
            ),
            FieldRule(
                rule_id="rule-contact-last-name",
                priority=36,
                intent="last_name",
                semantic_intent="name",
                locales=("pt-BR", "en"),
                exact_aliases=("sobrenome", "ultimo nome", "last name", "surname", "family name"),
                control_types=("text",),
                sensitivity="contact",
                source="master_profile",
                formatter=None,
                requires_manual=False,
                profile_key="contact.last_name",
            ),
            FieldRule(
                rule_id="rule-contact-name",
                priority=37,
                intent="name",
                semantic_intent="name",
                locales=("pt-BR", "en"),
                exact_aliases=("nome completo", "full name", "seu nome", "nome", "name"),
                control_types=("text",),
                sensitivity="contact",
                source="master_profile",
                formatter=None,
                requires_manual=False,
                profile_key="contact.name",
            ),
            FieldRule(
                rule_id="rule-contact-city",
                priority=38,
                intent="city",
                semantic_intent="location",
                locales=("pt-BR", "en"),
                exact_aliases=("cidade", "city"),
                control_types=("text", "select"),
                sensitivity="public",
                source="master_profile",
                formatter=None,
                requires_manual=False,
                profile_key="contact.city",
            ),
            FieldRule(
                rule_id="rule-contact-state",
                priority=39,
                intent="state",
                semantic_intent="location",
                locales=("pt-BR", "en"),
                exact_aliases=("estado", "uf", "state", "province"),
                control_types=("text", "select"),
                sensitivity="public",
                source="master_profile",
                formatter=None,
                requires_manual=False,
                profile_key="contact.state",
            ),
            FieldRule(
                rule_id="rule-contact-country",
                priority=40,
                intent="country",
                semantic_intent="location",
                locales=("pt-BR", "en"),
                exact_aliases=("pais", "country"),
                control_types=("text", "select"),
                sensitivity="public",
                source="master_profile",
                formatter=None,
                requires_manual=False,
                profile_key="contact.country",
            ),
            FieldRule(
                rule_id="rule-contact-location",
                priority=41,
                intent="location",
                semantic_intent="location",
                locales=("pt-BR", "en"),
                exact_aliases=("localizacao", "location", "onde mora", "residencia"),
                control_types=("text", "textarea", "select"),
                sensitivity="public",
                source="master_profile",
                formatter=None,
                requires_manual=False,
                profile_key="contact.location",
            ),
            FieldRule(
                rule_id="rule-pref-availability",
                priority=42,
                intent="availability",
                semantic_intent="availability",
                locales=("pt-BR", "en"),
                exact_aliases=("disponibilidade", "aviso previo", "notice period", "availability", "start date", "inicio"),
                control_types=("text", "textarea", "select", "radio", "date"),
                sensitivity="public",
                source="master_profile",
                formatter=None,
                requires_manual=False,
                profile_key="preferences.availability",
                arsenal_category="availability",
            ),
            FieldRule(
                rule_id="rule-pref-work-auth",
                priority=43,
                intent="work_authorization",
                semantic_intent="work_authorization",
                locales=("pt-BR", "en"),
                exact_aliases=(
                    "autorizacao de trabalho",
                    "work authorization",
                    "visa sponsorship",
                    "autorizado a trabalhar",
                    "autorizacao para trabalhar",
                    "visto",
                ),
                control_types=("text", "textarea", "select", "radio"),
                sensitivity="public",
                source="master_profile",
                formatter=None,
                requires_manual=False,
                profile_key="preferences.work_authorization",
                arsenal_category="work_authorization",
            ),
            FieldRule(
                rule_id="rule-english-proficiency",
                priority=44,
                intent="english_proficiency",
                semantic_intent="english_proficiency",
                locales=("pt-BR", "en"),
                exact_aliases=(
                    "nivel de ingles",
                    "ingles",
                    "english",
                    "english proficiency",
                    "english level",
                    "fluencia em ingles",
                ),
                control_types=("text", "textarea", "select", "radio"),
                sensitivity="public",
                source="master_profile",
                formatter=None,
                requires_manual=False,
                profile_key="languages.english",
                arsenal_category="english_proficiency",
            ),
            # Priority 60-79: Arsenal semantic categories (reusable vetted answers)
            FieldRule(
                rule_id="rule-arsenal-why-company",
                priority=60,
                intent="motivation",
                semantic_intent="motivation",
                locales=("pt-BR", "en"),
                exact_aliases=(
                    "por que quer trabalhar",
                    "por que esta empresa",
                    "motivacao",
                    "why do you want",
                    "why this company",
                    "why us",
                ),
                control_types=_ALL_TEXT_CONTROLS,
                sensitivity="public",
                source="arsenal",
                formatter=None,
                requires_manual=False,
                arsenal_category="why_company",
            ),
            FieldRule(
                rule_id="rule-arsenal-finops",
                priority=61,
                intent="case",
                semantic_intent="case",
                locales=("pt-BR", "en"),
                exact_aliases=("finops", "reducao de custos", "cloud impact", "custo em nuvem"),
                control_types=_ALL_TEXT_CONTROLS,
                sensitivity="public",
                source="arsenal",
                formatter=None,
                requires_manual=False,
                arsenal_category="finops_cloud_impact",
            ),
            FieldRule(
                rule_id="rule-arsenal-technical-challenge",
                priority=62,
                intent="case",
                semantic_intent="case",
                locales=("pt-BR", "en"),
                exact_aliases=(
                    "desafio tecnico",
                    "contribuicao tecnica",
                    "experiencia com",
                    "technical challenge",
                    "greatest technical",
                    "case tecnico",
                ),
                control_types=_ALL_TEXT_CONTROLS,
                sensitivity="public",
                source="arsenal",
                formatter=None,
                requires_manual=False,
                arsenal_category="technical_challenge",
            ),
            FieldRule(
                rule_id="rule-arsenal-summary",
                priority=63,
                intent="summary",
                semantic_intent="summary",
                locales=("pt-BR", "en"),
                exact_aliases=(
                    "resumo profissional",
                    "fale sobre voce",
                    "sobre voce",
                    "professional summary",
                    "about yourself",
                    "summary",
                ),
                control_types=_ALL_TEXT_CONTROLS,
                sensitivity="public",
                source="arsenal",
                formatter=None,
                requires_manual=False,
                arsenal_category="professional_summary",
            ),
            FieldRule(
                rule_id="rule-generic-date",
                priority=75,
                intent="date",
                semantic_intent="availability",
                locales=("pt-BR", "en"),
                exact_aliases=("data", "date", "quando"),
                control_types=("date",),
                sensitivity="public",
                source="pending_approval",
                formatter="date",
                requires_manual=True,
            ),
            FieldRule(
                rule_id="rule-arsenal-custom-fallback",
                priority=99,
                intent="unknown",
                semantic_intent="unknown",
                locales=("pt-BR", "en"),
                exact_aliases=(),
                control_types=_TEXTLIKE_CONTROLS + ("select", "radio", "checkbox", "custom_combobox", "unsupported"),
                sensitivity="public",
                source="arsenal",
                formatter=None,
                requires_manual=False,
                arsenal_category="custom",
            ),
        ),
        key=lambda r: (r.priority, r.rule_id),
    )
)


def match_field_rule(
    label: str,
    control_kind: str,
    *,
    category_hint: str | None = None,
) -> FieldRule:
    """Resolve the highest-precedence ordered rule for a control label and kind."""
    norm = normalize_rule_text(label)

    # Control-type / classifier forced high-precedence checks
    if control_kind == "file":
        if category_hint == "resume_file" or (
            category_hint is None and any(_phrase_matches(norm, a) for a in ("curriculo", "curriculum", "cv", "resume"))
        ):
            return next(r for r in ORDERED_FIELD_RULES if r.rule_id == "rule-resume-file")
        if category_hint == "non_resume_file" or any(
            _phrase_matches(norm, a)
            for a in next(r for r in ORDERED_FIELD_RULES if r.rule_id == "rule-non-resume-file").exact_aliases
        ):
            return next(r for r in ORDERED_FIELD_RULES if r.rule_id == "rule-non-resume-file")

    if control_kind in {"checkbox", "radio"} and category_hint == "legal_consent":
        return next(r for r in ORDERED_FIELD_RULES if r.rule_id == "rule-legal-consent")

    if control_kind == "email":
        return next(r for r in ORDERED_FIELD_RULES if r.rule_id == "rule-contact-email")
    if control_kind == "tel":
        return next(r for r in ORDERED_FIELD_RULES if r.rule_id == "rule-contact-phone")

    for rule in ORDERED_FIELD_RULES:
        if rule.rule_id == "rule-arsenal-custom-fallback":
            continue
        if rule.control_types and control_kind not in rule.control_types:
            continue
        if any(_phrase_matches(norm, alias) for alias in rule.exact_aliases):
            return rule

    if control_kind == "date":
        return next(r for r in ORDERED_FIELD_RULES if r.rule_id == "rule-generic-date")
    return next(r for r in ORDERED_FIELD_RULES if r.rule_id == "rule-arsenal-custom-fallback")


def extract_english_proficiency(doc: MasterProfileDocument) -> str | None:
    """Extract verified non-empty English proficiency from reviewed MasterProfile languages."""
    for lang in doc.languages:
        key = normalize_rule_text(lang.name)
        if key in {"ingles", "english", "ingl"}:
            lvl = (lang.proficiency or "").strip()
            if lvl and normalize_rule_text(lvl) not in {"unknown", "desconhecido", "nao informado"}:
                return f"{lang.name.strip()} — {lvl}" if lang.name.strip() else lvl
    return None


def extract_canonical_literals(doc: MasterProfileDocument) -> dict[str, str]:
    """Return literal verified candidate scalars from MasterProfileDocument (no fabrication)."""
    out: dict[str, str] = {}
    c = doc.contact
    p = doc.preferences

    if c.name and c.name.strip():
        full_name = c.name.strip()
        parts = full_name.split()
        out["contact.name"] = full_name
        if parts:
            out["contact.first_name"] = parts[0]
        if len(parts) >= 2:
            out["contact.last_name"] = " ".join(parts[1:])
    if c.email and c.email.strip():
        out["contact.email"] = c.email.strip()
    if c.phone and c.phone.strip():
        out["contact.phone"] = c.phone.strip()
    if c.city and c.city.strip():
        out["contact.city"] = c.city.strip()
    if c.state and c.state.strip():
        out["contact.state"] = c.state.strip()
    if c.country and c.country.strip():
        out["contact.country"] = c.country.strip()
    loc_parts = [x.strip() for x in (c.city, c.state, c.country) if x and x.strip()]
    if loc_parts:
        out["contact.location"] = ", ".join(loc_parts)
    elif p.location and p.location.strip():
        out["contact.location"] = p.location.strip()
    if c.links.linkedin and c.links.linkedin.strip():
        out["contact.links.linkedin"] = c.links.linkedin.strip()
    if c.links.github and c.links.github.strip():
        out["contact.links.github"] = c.links.github.strip()
    if c.links.portfolio and c.links.portfolio.strip():
        out["contact.links.portfolio"] = c.links.portfolio.strip()

    if p.availability and p.availability.strip():
        out["preferences.availability"] = p.availability.strip()
    if p.work_authorization and p.work_authorization.strip():
        out["preferences.work_authorization"] = p.work_authorization.strip()

    eng = extract_english_proficiency(doc)
    if eng:
        out["languages.english"] = eng

    for sa in doc.standard_answers:
        norm_lbl = normalize_rule_text(f"{sa.label} {sa.question_signature}")
        if sa.answer and sa.answer.strip() and any(
            _phrase_matches(norm_lbl, a) for a in ("cep", "zip code", "postal code", "codigo postal")
        ):
            out["contact.postal_code"] = sa.answer.strip()
            break

    return out


def build_verified_slot_values(
    doc: MasterProfileDocument,
    *,
    job_company: str = "",
    job_title: str = "",
    job_description: str = "",
) -> dict[str, str]:
    """Derive bounded, verified {{slot}} values from pinned job record and MasterProfile skills."""
    values: dict[str, str] = {}
    clean_company = " ".join(job_company.split()).strip()[:160]
    clean_title = " ".join(job_title.split()).strip()[:160]
    if clean_company:
        values["company_name"] = clean_company
    if clean_title:
        values["role_title"] = clean_title

    job_norm = normalize_rule_text(f"{job_title} {job_description}")
    matched_skills: list[str] = []
    seen_norm: set[str] = set()
    for skill in doc.skills:
        s_name = " ".join((skill.name or "").split()).strip()
        s_norm = normalize_rule_text(s_name)
        if not s_norm or s_norm in seen_norm:
            continue
        if _phrase_matches(job_norm, s_norm):
            matched_skills.append(s_name)
            seen_norm.add(s_norm)
        if len(matched_skills) >= 3:
            break
    if matched_skills:
        values["tech_stack_highlight"] = ", ".join(matched_skills[:3])
    return values
