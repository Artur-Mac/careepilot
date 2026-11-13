"""pt-BR + EN browser discovery query generator focused on Recife, PE and Brazil/Remote jobs."""

from __future__ import annotations

from dataclasses import dataclass

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
) -> list[dict[str, str]]:
    """Build clean keyword+location tuples suitable for portal search boxes (LinkedIn/Gupy/InfoJobs)."""
    clean_roles = [r.strip() for r in roles if r.strip()]
    if not clean_roles:
        return []
    pref_loc = (location or "Recife, PE").strip()
    entries: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    for role in clean_roles[:4]:
        if not remote_only and pref_loc:
            key = (role.lower(), pref_loc.lower(), "local")
            if key not in seen:
                seen.add(key)
                entries.append({"keywords": role, "location": pref_loc, "mode": "local"})
        if remote_only or include_remote:
            key = (role.lower(), "brasil", "remote")
            if key not in seen:
                seen.add(key)
                entries.append({"keywords": role, "location": "Brasil", "mode": "remote"})
        if len(entries) >= max_queries:
            break
    return entries[:max_queries]


def generate_search_queries(config: QueryGenerationConfig) -> list[str]:
    """Generate concise deduplicated PT-BR/EN Google queries for Recife and Brazil-eligible remote jobs."""
    raw_queries: list[str] = []
    roles = [r.strip() for r in config.roles if r.strip()] or ["Data Engineer", "Engenheiro de Dados"]
    locs = [loc.strip() for loc in config.locations if loc.strip()] or ["Recife PE", "Brasil"]

    neg = " -senior -sênior -staff -principal -lead" if config.exclude_senior else ""

    for role in roles[:4]:
        quoted_role = f'"{role}"' if " " in role and not role.startswith('"') else role
        if config.remote_only:
            raw_queries.append(f'{quoted_role} ("remoto" OR "remote") ("Brasil" OR "Brazil" OR "LATAM"){neg}'.strip())
        else:
            raw_queries.append(f'{quoted_role} "{locs[0]}"{neg}'.strip())
            if config.include_remote:
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
