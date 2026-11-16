"""pt-BR + EN browser discovery query generator focused on Recife, PE and Brazil/Remote jobs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.app.schemas.profile import Archetype

DEFAULT_SENIORITY_SYNONYMS: dict[str, tuple[str, ...]] = {
    "junior": ("junior", "júnior", "entry level"),
    "intern": ("estágio", "estagio", "internship"),
    "trainee": ("trainee",),
    "mid": ("pleno", "mid"),
}

CORE_TECH_SKILLS: tuple[str, ...] = (
    "SQL",
    "Python",
    "Azure",
    "Databricks",
    "Power BI",
    "FastAPI",
    "ETL",
)

ATS_SITE_OPERATORS: tuple[str, ...] = (
    "site:gupy.io",
    "site:jobs.lever.co",
    "site:boards.greenhouse.io",
    "site:jobs.ashbyhq.com",
)

ROLE_ARCHETYPE_ALIASES: dict[str, dict[str, Any]] = {
    "data_engineer": {
        "pt-BR": ["Engenheiro de Dados", "Engenheira de Dados", "Engenharia de Dados"],
        "en": ["Data Engineer", "Analytics Engineer"],
        "skills": ["Python", "SQL", "Spark", "Airflow", "dbt", "ETL", "Databricks", "BigQuery"],
    },
    "backend_python": {
        "pt-BR": ["Desenvolvedor Backend Python", "Engenheiro Backend Python", "Dev Python"],
        "en": ["Python Backend Engineer", "Backend Developer Python", "Software Engineer Python"],
        "skills": ["Python", "FastAPI", "Django", "PostgreSQL", "Docker", "SQL", "Redis"],
    },
    "ml_engineer": {
        "pt-BR": ["Engenheiro de Machine Learning", "Cientista de Dados", "Engenheiro de IA"],
        "en": ["Machine Learning Engineer", "Data Scientist", "AI Engineer"],
        "skills": ["Python", "PyTorch", "Scikit-learn", "MLOps", "SQL", "Docker"],
    },
    "intern_junior": {
        "pt-BR": ["Estágio em Dados", "Pessoa Desenvolvedora Júnior", "Trainee Tecnologia"],
        "en": ["Data Engineering Intern", "Junior Software Engineer", "Junior Data Analyst"],
        "skills": ["Python", "SQL", "Git", "Power BI"],
    },
}


def infer_role_archetype(role_text: str) -> Archetype:
    low = role_text.lower()
    if any(k in low for k in ("data engineer", "engenheir", "dados", "analytics", "etl", "bi")):
        if any(k in low for k in ("estágio", "estagio", "intern", "trainee")):
            return "intern_junior"
        if any(k in low for k in ("cientista", "scientist", "machine learning", "ml", "ia", "ai")):
            return "ml_engineer"
        return "data_engineer"
    if any(k in low for k in ("machine learning", "cientista", "scientist", "ml", "ai", "ia")):
        return "ml_engineer"
    if any(k in low for k in ("estágio", "estagio", "intern", "trainee", "júnior", "junior")):
        return "intern_junior"
    if any(k in low for k in ("backend", "back-end", "python", "software", "desenvolvedor", "dev")):
        return "backend_python"
    return "all"


@dataclass(frozen=True, slots=True)
class QueryGenerationConfig:
    roles: list[str]
    skills: list[str]
    locations: list[str]
    remote_only: bool = False
    include_remote: bool = True
    seniority_levels: list[str] | None = None
    include_ats_sites: bool = True
    exclude_senior: bool = True
    max_queries: int = 10


def generate_portal_queries(
    roles: list[str],
    *,
    location: str | None = None,
    remote_only: bool = False,
    include_remote: bool = True,
    max_queries: int = 6,
    skills: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Build clean keyword+location tuples suitable for portal search boxes (LinkedIn/Gupy/InfoJobs)."""
    clean_roles = [r.strip() for r in roles if r.strip()]
    if not clean_roles:
        return []
    pref_loc = (location or "Recife, PE").strip()
    disc_skills = [s.strip() for s in (skills or []) if s.strip()][:2]
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    for role in clean_roles[:4]:
        archetype = infer_role_archetype(role)
        role_skills = [
            s for s in disc_skills if s.lower() not in role.lower()
        ][:2]
        variants: list[tuple[str, list[str], bool]] = []
        if role_skills:
            variants.append((f"{role} {' '.join(role_skills)}", role_skills, False))
        variants.append((role, [], True))

        for kw_text, chosen_skills, is_fallback in variants:
            if not remote_only and pref_loc:
                key = (kw_text.lower(), pref_loc.lower(), "recife")
                if key not in seen:
                    seen.add(key)
                    entries.append(
                        {
                            "keywords": kw_text,
                            "role": role,
                            "archetype": archetype,
                            "skills": chosen_skills,
                            "role_only_fallback": is_fallback,
                            "location": pref_loc,
                            "mode": "local",
                            "query_mode": "recife",
                            "filters": {
                                "workplace": ["onsite", "hybrid"],
                                "city": "Recife",
                                "state": "PE",
                                "country": "BR",
                            },
                            "reason": (
                                f"Fallback apenas com cargo '{role}' em {pref_loc} para ampliar cobertura."
                                if is_fallback
                                else f"Cargo '{role}' combinado com até 2 skills discriminativas ({', '.join(chosen_skills)}) em {pref_loc}."
                            ),
                        }
                    )
            if remote_only or include_remote:
                key = (kw_text.lower(), "brasil", "remote")
                if key not in seen:
                    seen.add(key)
                    entries.append(
                        {
                            "keywords": kw_text,
                            "role": role,
                            "archetype": archetype,
                            "skills": chosen_skills,
                            "role_only_fallback": is_fallback,
                            "location": "Brasil",
                            "mode": "remote",
                            "query_mode": "remote",
                            "filters": {
                                "workplace": ["remote"],
                                "country": "BR",
                                "brazil_eligible": True,
                            },
                            "reason": (
                                f"Fallback apenas com cargo '{role}' para vagas remotas elegíveis do Brasil."
                                if is_fallback
                                else f"Cargo '{role}' + skills ({', '.join(chosen_skills)}) para vagas remotas no Brasil."
                            ),
                        }
                    )
            if len(entries) >= max_queries:
                break
        if len(entries) >= max_queries:
            break
    return entries[:max_queries]


def build_bounded_search_plan(
    *,
    roles: list[str],
    skills: list[str],
    location: str | None = "Recife, PE",
    remote_only: bool = False,
    include_remote: bool = True,
    portal_keys: list[str] | None = None,
    locale: str | None = None,
    target_role_keys: list[str] | None = None,
    profile_revision: int = 1,
    tuning_revision: int = 0,
    keyword_weights: dict[str, int] | None = None,
    blocked_portals: dict[str, str] | None = None,
    disabled_portals: set[str] | None = None,
) -> dict[str, Any]:
    """Build deterministic bounded search plan with explanations, filters, and skipped scopes."""
    supported_portals = ("linkedin", "gupy", "infojobs", "google")
    requested_portals = [
        p for p in (portal_keys or list(supported_portals)) if p in supported_portals
    ] or list(supported_portals)

    expanded_roles: list[str] = []
    if target_role_keys:
        for rk in target_role_keys:
            alias_cfg = ROLE_ARCHETYPE_ALIASES.get(rk)
            if alias_cfg:
                if locale == "en":
                    expanded_roles.extend(alias_cfg["en"][:2])
                elif locale == "pt-BR":
                    expanded_roles.extend(alias_cfg["pt-BR"][:2])
                else:
                    expanded_roles.append(alias_cfg["pt-BR"][0])
                    expanded_roles.append(alias_cfg["en"][0])
            elif rk.strip():
                expanded_roles.append(rk.strip())

    for r in roles:
        clean_r = r.strip()
        if clean_r and clean_r not in expanded_roles:
            expanded_roles.append(clean_r)
            arch = infer_role_archetype(clean_r)
            alias_cfg = ROLE_ARCHETYPE_ALIASES.get(arch)
            if alias_cfg:
                if locale == "en":
                    for en_alias in alias_cfg["en"][:1]:
                        if en_alias.lower() != clean_r.lower() and en_alias not in expanded_roles:
                            expanded_roles.append(en_alias)
                elif locale == "pt-BR":
                    for pt_alias in alias_cfg["pt-BR"][:1]:
                        if pt_alias.lower() != clean_r.lower() and pt_alias not in expanded_roles:
                            expanded_roles.append(pt_alias)

    weights = keyword_weights or {}
    discriminative_skills = [s.strip() for s in skills if s.strip()][:2]
    portal_queries = generate_portal_queries(
        expanded_roles[:4],
        location=location,
        remote_only=remote_only,
        include_remote=include_remote,
        max_queries=6,
        skills=discriminative_skills,
    )
    google_queries = generate_search_queries(
        QueryGenerationConfig(
            roles=expanded_roles[:4],
            skills=discriminative_skills,
            locations=[location or "Recife, PE", "Brasil"],
            remote_only=remote_only,
            include_remote=include_remote,
            max_queries=8,
        )
    )

    skipped_scopes: list[dict[str, str]] = []
    active_portals: list[str] = []
    blocked_map = blocked_portals or {}
    disabled_set = disabled_portals or set()

    for portal in requested_portals:
        if portal in disabled_set:
            skipped_scopes.append(
                {
                    "portal_key": portal,
                    "reason_code": "connector_disabled",
                    "reason": f"Conector '{portal}' desativado nas configurações.",
                }
            )
            continue
        if portal in blocked_map:
            skipped_scopes.append(
                {
                    "portal_key": portal,
                    "reason_code": blocked_map[portal],
                    "reason": f"Portal '{portal}' requer login ou verificação no Pré-voo ({blocked_map[portal]}).",
                }
            )
            continue
        active_portals.append(portal)

    if remote_only:
        skipped_scopes.append(
            {
                "portal_key": "all",
                "reason_code": "recife_onsite_skipped_remote_only",
                "reason": "Modo somente remotas ativo: consultas presenciais/híbridas em Recife foram omitidas.",
            }
        )
    elif not include_remote:
        skipped_scopes.append(
            {
                "portal_key": "all",
                "reason_code": "remote_skipped_local_only",
                "reason": "Modo local ativo: consultas remotas nacionais foram omitidas.",
            }
        )

    chosen_terms_reasons = [
        {
            "term": r,
            "kind": "role",
            "archetype": infer_role_archetype(r),
            "reason": "Cargo alvo selecionado do Perfil mestre ou filtro explícito.",
        }
        for r in expanded_roles[:4]
    ] + [
        {
            "term": s,
            "kind": "skill",
            "weight": weights.get(s.lower(), 0),
            "reason": (
                f"Habilidade priorizada pelo feedback (peso +{weights.get(s.lower(), 0)})."
                if weights.get(s.lower(), 0) > 0
                else "Habilidade discriminativa do Perfil mestre."
            ),
        }
        for s in discriminative_skills
    ]

    return {
        "profile_revision": profile_revision,
        "tuning_revision": tuning_revision,
        "locale": locale or "pt-BR",
        "portal_keys": active_portals,
        "roles": expanded_roles[:4],
        "discriminative_skills": discriminative_skills,
        "portal_queries": portal_queries,
        "queries": google_queries,
        "count": len(google_queries),
        "chosen_terms": chosen_terms_reasons,
        "skipped_scopes": skipped_scopes,
        "limits": {
            "max_result_pages_per_portal": 2,
            "max_detail_pages_total": 20,
            "run_timeout_seconds": 120,
            "min_navigation_pacing_seconds": 2,
        },
    }


def generate_search_queries(config: QueryGenerationConfig) -> list[str]:
    """Generate concise deduplicated PT-BR/EN Google queries for Recife and Brazil-eligible remote jobs."""
    raw_queries: list[str] = []
    roles = [r.strip() for r in config.roles if r.strip()] or ["Data Engineer", "Engenheiro de Dados"]
    locs = [loc.strip() for loc in config.locations if loc.strip()] or ["Recife PE", "Brasil"]
    top_skills = [s.strip() for s in config.skills if s.strip()][:2]

    neg = " -senior -sênior -staff -principal -lead" if config.exclude_senior else ""

    for role in roles[:4]:
        quoted_role = f'"{role}"' if " " in role and not role.startswith('"') else role
        skill_clause = f" {' '.join(top_skills)}" if top_skills else ""
        if config.remote_only:
            if skill_clause:
                raw_queries.append(
                    f'{quoted_role}{skill_clause} ("remoto" OR "remote") ("Brasil" OR "Brazil" OR "LATAM"){neg}'.strip()
                )
            raw_queries.append(f'{quoted_role} ("remoto" OR "remote") ("Brasil" OR "Brazil" OR "LATAM"){neg}'.strip())
        else:
            if skill_clause:
                raw_queries.append(f'{quoted_role}{skill_clause} "{locs[0]}"{neg}'.strip())
            raw_queries.append(f'{quoted_role} "{locs[0]}"{neg}'.strip())
            if config.include_remote:
                if skill_clause:
                    raw_queries.append(
                        f'{quoted_role}{skill_clause} ("remoto" OR "home office") ("Brasil" OR "LATAM"){neg}'.strip()
                    )
                raw_queries.append(
                    f'{quoted_role} ("remoto" OR "home office") ("Brasil" OR "LATAM"){neg}'.strip()
                )

    if config.include_ats_sites:
        primary = roles[0]
        quoted_primary = f'"{primary}"' if " " in primary and not primary.startswith('"') else primary
        geo_clause = '("remoto" OR "Brasil")' if config.remote_only else f'("{locs[0]}" OR "remoto Brasil")'
        for op in ATS_SITE_OPERATORS[:3]:
            raw_queries.append(f"{op} {quoted_primary} {geo_clause}{neg}".strip())
        raw_queries.append(f'"trabalhe conosco" {quoted_primary} {geo_clause}{neg}'.strip())

    seen: set[str] = set()
    deduped: list[str] = []
    for q in raw_queries:
        cleaned = " ".join(q.split())
        norm = cleaned.lower()
        if norm not in seen:
            seen.add(norm)
            deduped.append(cleaned)
        if len(deduped) >= config.max_queries:
            break
    return deduped

