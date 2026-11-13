"""Portal adapter registry hints (populated fully in Phase 3 step-fill copilot)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AtsAdapterSpec:
    name: str
    domain_patterns: tuple[str, ...]
    notes: str


ATS_ADAPTERS: dict[str, AtsAdapterSpec] = {
    "linkedin": AtsAdapterSpec(
        name="linkedin",
        domain_patterns=("linkedin.com", "www.linkedin.com"),
        notes="LinkedIn Easy Apply modal; usuario avança e envia no Chrome.",
    ),
    "gupy": AtsAdapterSpec(
        name="gupy",
        domain_patterns=("gupy.io", "portal.gupy.io"),
        notes="Gupy fluxo por etapas; conta/perfil global separado da vaga.",
    ),
    "infojobs": AtsAdapterSpec(
        name="infojobs",
        domain_patterns=("infojobs.com.br", "www.infojobs.com.br"),
        notes="InfoJobs candidatura por etapas; usuário avança no Chrome.",
    ),
    "greenhouse": AtsAdapterSpec(
        name="greenhouse",
        domain_patterns=("boards.greenhouse.io", "job-boards.greenhouse.io"),
        notes="ATS externo genérico; usuário revisa e envia no Chrome.",
    ),
    "lever": AtsAdapterSpec(
        name="lever",
        domain_patterns=("jobs.lever.co", "jobs.eu.lever.co"),
        notes="ATS externo genérico; usuário revisa e envia no Chrome.",
    ),
}
