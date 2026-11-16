"""Profile-based match explanations and feedback projection."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.connectors.search.query_generator import infer_role_archetype
from backend.app.models.tables import JobDiscoveryFeedbackRow, JobPosting, SearchTuningRuleRow
from backend.app.schemas.profile import MasterProfileDocument

_MATCH_SCORE_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}


def round_half_up(val: float | Decimal) -> int:
    return int(Decimal(str(val)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@lru_cache(maxsize=8192)
def match_key(value: str) -> str:
    return " ".join("".join(
        char for char in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(char)
    ).split())


@lru_cache(maxsize=2048)
def _phrase_re(phrase: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(phrase)}(?!\w)")


def includes(text: str, phrase: str) -> bool:
    return bool(phrase and _phrase_re(phrase).search(text))


_SENIORITY_ORDER: dict[str, int] = {
    "intern": 0,
    "estagio": 0,
    "trainee": 1,
    "junior": 2,
    "entry_level": 2,
    "entry level": 2,
    "mid": 3,
    "pleno": 3,
    "senior": 4,
    "lead": 5,
    "staff": 5,
    "principal": 5,
}

_ADJACENT_ARCHETYPES: dict[str, frozenset[str]] = {
    "data_engineer": frozenset({"backend_python", "ml_engineer", "intern_junior"}),
    "backend_python": frozenset({"data_engineer", "ml_engineer", "intern_junior"}),
    "ml_engineer": frozenset({"data_engineer", "backend_python", "intern_junior"}),
    "intern_junior": frozenset({"data_engineer", "backend_python", "ml_engineer"}),
}


_REQUIREMENT_HEADING = re.compile(
    r"^(?P<label>requisitos(?: obrigatorios| desejaveis)?|qualificacoes|diferenciais|"
    r"requirements|required qualifications|preferred qualifications|nice to have)\s*:?$"
)
_SECTION_END = re.compile(
    r"^(responsabilidades|beneficios|sobre (?:nos|a empresa)|responsibilities|benefits|about us)\s*:?$"
)
_TECH_TERMS = (
    "Python", "SQL", "PySpark", "Apache Spark", "FastAPI", "Databricks", "Airflow",
    "dbt", "Docker", "Kubernetes", "PostgreSQL", "AWS", "Azure", "GCP",
    "Pandas", "Power BI", "Terraform", "JavaScript", "TypeScript", "React", "Java",
    "C++", "C#", ".NET", "Machine Learning", "Snowflake", "Scala", "Go", "Rust",
)


def extract_requirements(description: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    section = ""
    for line in description.splitlines()[:500]:
        raw = line.strip().lstrip("-*• ").strip()
        key = match_key(raw)
        heading = _REQUIREMENT_HEADING.fullmatch(key)
        if heading:
            section = "preferred" if any(word in key for word in ("desejaveis", "diferenciais", "preferred", "nice")) else "required"
            continue
        if _SECTION_END.fullmatch(key):
            section = ""
        if not section or not raw:
            continue
        terms = [match_key(term) for term in _TECH_TERMS if includes(key, match_key(term))]
        result.append({"text": raw[:500], "kind": section, "skill_keys": terms})
        if len(result) >= 80:
            break
    return result


def match_job(
    job: JobPosting,
    doc: MasterProfileDocument,
    weights: dict[str, int],
    seniority_weights: dict[str, int] | None = None,
    *,
    negative_keywords: list[str] | None = None,
    profile_revision: int = 1,
    tuning_revision: int = 0,
) -> dict[str, Any]:
    elig_dict = job.eligibility_json or {}
    cache_key = (
        job.id,
        job.description_hash,
        job.title,
        job.seniority,
        job.employment_type,
        elig_dict.get("brazil_eligible"),
        elig_dict.get("status"),
        profile_revision,
        tuning_revision,
        tuple(sorted(weights.items())),
        tuple(sorted((seniority_weights or {}).items())),
        tuple(negative_keywords or ()),
        tuple(s.id for s in doc.skills),
        tuple(doc.preferences.target_roles),
        tuple(doc.preferences.seniority),
        tuple(doc.preferences.contract_types),
    )
    cached = _MATCH_SCORE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    text = match_key(f"{job.title} {job.description}")
    relevant_skills = {
        match_key(skill.name): skill.name
        for skill in doc.skills if includes(text, match_key(skill.name))
    }
    matched_claim_ids = [
        skill.id for skill in doc.skills if match_key(skill.name) in relevant_skills
    ]
    requirement_skills: dict[str, set[str]] = {}
    for requirement in job.requirements_json or []:
        kind = requirement.get("kind", "unknown")
        requirement_skills.setdefault(kind, set()).update(
            match_key(skill) for skill in requirement.get("skill_keys", []) if isinstance(skill, str) and skill.strip()
        )
    expected_skills = (
        requirement_skills.get("required") or requirement_skills.get("preferred")
        or requirement_skills.get("unknown") or set()
    )

    def weight_factor(key: str) -> float:
        w = max(-5, min(5, int(weights.get(key, 0))))
        return max(0.5, min(1.5, 1.0 + (w / 10.0)))

    matched_skills = expected_skills.intersection(relevant_skills) if expected_skills else set(relevant_skills)
    denominator = sum(weight_factor(key) for key in expected_skills)
    if denominator > 0:
        raw_skill = 40.0 * sum(weight_factor(key) for key in matched_skills) / denominator
        skill_points = max(0, min(40, round_half_up(raw_skill)))
    else:
        raw_fallback = sum(max(1.0, 8.0 * weight_factor(key)) for key in matched_skills)
        skill_points = max(0, min(40, round_half_up(raw_fallback)))

    title_key = match_key(job.title)
    roles = [
        role for role in doc.preferences.target_roles
        if includes(title_key, match_key(role))
    ]
    job_archetype = infer_role_archetype(job.title)
    target_archetypes = {infer_role_archetype(r) for r in doc.preferences.target_roles if r.strip()}
    if roles or (job_archetype != "all" and job_archetype in target_archetypes):
        role_points = 25
        role_reason = f"Correspondência exata de cargo/arquétipo ({job_archetype})."
    elif job_archetype != "all" and any(
        job_archetype in _ADJACENT_ARCHETYPES.get(ta, frozenset()) for ta in target_archetypes
    ):
        role_points = 12
        role_reason = f"Arquétipo adjacente ({job_archetype}) aos cargos alvo."
    else:
        role_points = 0
        role_reason = "Sem correspondência direta com os cargos alvo."

    seniority = match_key(job.seniority or "")
    pref_seniorities = [match_key(s) for s in doc.preferences.seniority if s.strip()]
    sen_base = 0
    if seniority and seniority in pref_seniorities:
        sen_base = 15
    elif seniority in _SENIORITY_ORDER and pref_seniorities:
        job_ord = _SENIORITY_ORDER[seniority]
        if any(
            ps in _SENIORITY_ORDER and abs(_SENIORITY_ORDER[ps] - job_ord) == 1
            for ps in pref_seniorities
        ):
            sen_base = 7
    sen_bias = max(-5, min(5, int((seniority_weights or {}).get(seniority, 0))))
    sen_points = max(0, min(15, sen_base + sen_bias))

    elig_dict = job.eligibility_json or {}
    eligible = elig_dict.get("brazil_eligible") == "eligible" or elig_dict.get("status") == "eligible"
    loc_points = 15 if eligible else 0
    contract_points = (
        5
        if job.employment_type and match_key(job.employment_type) in map(match_key, doc.preferences.contract_types)
        else 0
    )

    active_neg_keywords = list(negative_keywords or [])
    for k, w in weights.items():
        if w <= -4 and k not in active_neg_keywords:
            active_neg_keywords.append(k)
    matched_neg_keywords = [kw for kw in active_neg_keywords if includes(text, match_key(kw))]
    penalty_points = min(15, 3 * len(matched_neg_keywords))

    components = {
        "skills": skill_points,
        "role": role_points,
        "seniority": sen_points,
        "location": loc_points,
        "contract": contract_points,
    }
    raw_total = sum(components.values()) - penalty_points
    final_score = max(0, min(100, round_half_up(raw_total)))

    red_flags: list[str] = []
    if elig_dict.get("brazil_eligible") == "restricted":
        red_flags.append(elig_dict.get("reason") or "Elegibilidade geográfica restrita.")
    if matched_neg_keywords:
        red_flags.append(f"Termos penalizados pelo seu histórico: {', '.join(matched_neg_keywords)}.")
    if sen_bias < 0:
        red_flags.append(f"Nível de senioridade '{seniority}' ajustado por feedback anterior ({sen_bias}).")

    reasons = [
        f"Skills: {skill_points}/40 pts ({len(matched_skills)} compatíveis)",
        f"Cargo: {role_points}/25 pts — {role_reason}",
        f"Senioridade: {sen_points}/15 pts",
        f"Localização: {loc_points}/15 pts",
        f"Contrato: {contract_points}/5 pts",
    ]
    if penalty_points:
        reasons.append(f"Penalidades: -{penalty_points} pts ({', '.join(matched_neg_keywords)})")

    result = {
        "score": final_score,
        "skills": [name for key, name in relevant_skills.items() if key in matched_skills],
        "missing_requirements": sorted(expected_skills - matched_skills),
        "roles": roles,
        "archetype": job_archetype,
        "components": components,
        "penalties": penalty_points,
        "negative_matches": matched_neg_keywords,
        "reasons": reasons,
        "red_flags": red_flags,
        "matched_claim_ids": matched_claim_ids,
        "profile_revision": profile_revision,
        "tuning_revision": tuning_revision,
    }
    if len(_MATCH_SCORE_CACHE) >= 12000:
        _MATCH_SCORE_CACHE.clear()
    _MATCH_SCORE_CACHE[cache_key] = result
    return result


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["high_fit", "too_senior", "wrong_tech_stack", "onsite_outside_recife", "blacklist_company"]
    operation: Literal["apply", "undo"]
    request_id: str = Field(min_length=8, max_length=100)


def active_feedback(db: Session, user_id: str) -> dict[tuple[str, str], JobDiscoveryFeedbackRow]:
    events = db.scalars(select(JobDiscoveryFeedbackRow).where(
        JobDiscoveryFeedbackRow.user_id == user_id
    ).order_by(JobDiscoveryFeedbackRow.created_at, JobDiscoveryFeedbackRow.id))
    active: dict[tuple[str, str], JobDiscoveryFeedbackRow] = {}
    for event in events:
        key = (event.job_id, event.action)
        if event.operation == "apply":
            active[key] = event
        else:
            active.pop(key, None)
    return active


def rebuild_tuning(db: Session, user_id: str) -> None:
    weights: Counter[str] = Counter()
    wrong_stack_job_votes: dict[str, set[str]] = {}
    seniority_weights: Counter[str] = Counter()
    archetype_seniority_weights: dict[str, Counter[str]] = {}
    blocked = set()
    for (job_id, action), event in active_feedback(db, user_id).items():
        if action == "high_fit":
            for sk in set(event.evidence_json.get("skill_keys", [])):
                weights[sk] += 1
        if action == "wrong_tech_stack":
            for req in set(event.evidence_json.get("missing_requirements", [])):
                wrong_stack_job_votes.setdefault(req, set()).add(job_id)
                weights[req] -= 1
        if action == "too_senior" and event.evidence_json.get("seniority"):
            sen_key = str(event.evidence_json["seniority"])
            seniority_weights[sen_key] -= 2
            arch_key = str(event.evidence_json.get("archetype") or "all")
            archetype_seniority_weights.setdefault(arch_key, Counter())[sen_key] -= 2
        if action == "blacklist_company":
            blocked.add(event.evidence_json["company_key"])

    active_negative_keywords = sorted(
        req for req, voted_jobs in wrong_stack_job_votes.items() if len(voted_jobs) >= 2
    )
    sen_map: dict[str, dict[str, int]] = {
        "all": {key: max(-5, min(5, count)) for key, count in seniority_weights.items()}
    }
    for arch_k, cnt_map in sorted(archetype_seniority_weights.items()):
        if arch_k != "all":
            sen_map[arch_k] = {k: max(-5, min(5, v)) for k, v in cnt_map.items()}

    rules: dict[str, Any] = {
        "version": 1,
        "keyword_weights": {key: max(-5, min(5, count * 2)) for key, count in sorted(weights.items()) if count},
        "negative_keywords": active_negative_keywords,
        "seniority_weights": sen_map,
        "blocked_company_keys": sorted(blocked),
    }
    existing = db.get(SearchTuningRuleRow, user_id)
    if existing is None:
        db.add(SearchTuningRuleRow(user_id=user_id, rules_json=rules))
    elif existing.rules_json != rules:
        existing.rules_json = rules
        existing.revision += 1
