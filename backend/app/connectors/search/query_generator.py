"""pt-BR + EN search query generator with conservative synonyms, ATS site filters, and credit-saving dedup."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_SENIORITY_SYNONYMS: dict[str, tuple[str, ...]] = {
    "junior": ("junior", "júnior", "entry level", "graduate"),
    "intern": ("internship", "estágio", "estagiário"),
    "trainee": ("trainee", "graduate program"),
}

DEFAULT_ROLE_FAMILIES: dict[str, tuple[str, ...]] = {
    "data_engineer": ("\"Data Engineer\"", "\"Engenheiro de Dados\"", "\"Engenheira de Dados\"", "ETL"),
    "analytics_engineer": ("\"Analytics Engineer\"", "\"Engenheiro de Analytics\""),
    "data_analyst": ("\"Data Analyst\"", "\"Analista de Dados\"", "BI", "\"Power BI\""),
    "data_scientist": ("\"Data Scientist\"", "\"Cientista de Dados\"", "\"machine learning\""),
    "backend_python": ("\"Backend Python\"", "\"Software Engineer\" Python", "FastAPI"),
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
    "site:jobs.ashbyhq.com",
    "site:jobs.lever.co",
    "site:job-boards.greenhouse.io",
    "site:gupy.io",
)

NEGATIVE_SENIOR_TERMS = "-senior -sênior -staff -principal -lead -gerente -manager"


@dataclass(frozen=True, slots=True)
class QueryGenerationConfig:
    roles: list[str]
    skills: list[str]
    locations: list[str]
    remote_only: bool = False
    seniority_levels: list[str] | None = None
    include_ats_sites: bool = True
    exclude_senior: bool = True
    max_queries: int = 16


def generate_search_queries(config: QueryGenerationConfig) -> list[str]:
    """Generate deduplicated pt-BR and English search queries before spending paid API credits."""
    raw_queries: list[str] = []

    seniority_terms: list[str] = []
    for level in config.seniority_levels or ["junior", "trainee", "intern"]:
        syns = DEFAULT_SENIORITY_SYNONYMS.get(level.lower(), (level,))
        seniority_terms.extend(syns[:2])
    seniority_expr = "(" + " OR ".join(dict.fromkeys(seniority_terms)) + ")"

    roles = config.roles or [
        "Data Engineer",
        "Analytics Engineer",
        "Data Analyst",
        "Data Scientist",
        "Backend Python",
        "Software Engineer",
    ]
    skills = [s for s in (config.skills or list(CORE_TECH_SKILLS)) if s.strip()][:5]
    locs = config.locations or ["Brasil", "Brazil"]

    neg = f" {NEGATIVE_SENIOR_TERMS}" if config.exclude_senior else ""
    remote_part = " (remote OR remoto)" if config.remote_only else ""

    for role in roles[:6]:
        quoted_role = f'"{role}"' if " " in role and not role.startswith('"') else role
        skill_expr = f"({' OR '.join(skills[:3])})" if skills else ""
        loc_expr = f'"{locs[0]}"' if locs else ""
        base_q = " ".join(part for part in (quoted_role, seniority_expr, skill_expr, loc_expr) if part)
        raw_queries.append(f"{base_q}{remote_part}{neg}".strip())

    if config.include_ats_sites:
        primary_role = f'"{roles[0]}"' if " " in roles[0] else roles[0]
        for site_op in ATS_SITE_OPERATORS:
            raw_queries.append(
                f"{site_op} {primary_role} {seniority_expr}{remote_part}{neg}".strip()
            )

    # Deduplicate case-insensitively while preserving order to prevent wasted paid search credits.
    seen: set[str] = set()
    deduped: list[str] = []
    for q in raw_queries:
        norm = " ".join(q.split()).lower()
        if norm not in seen:
            seen.add(norm)
            deduped.append(" ".join(q.split()))
        if len(deduped) >= config.max_queries:
            break
    return deduped
