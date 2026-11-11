"""ATS field mapping hints and strictly allowlisted optional visual fallback planner."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

AllowedVisualAction = Literal["click", "scroll", "type", "select", "upload"]
ALLOWED_VISUAL_ACTIONS: frozenset[str] = frozenset({"click", "scroll", "type", "select", "upload"})


@dataclass(frozen=True, slots=True)
class AtsAdapterSpec:
    name: str
    domain_patterns: tuple[str, ...]
    automated_submit_permitted: bool
    notes: str


ATS_ADAPTERS: dict[str, AtsAdapterSpec] = {
    "fictional_ats": AtsAdapterSpec(
        name="fictional_ats",
        domain_patterns=("127.0.0.1", "localhost"),
        automated_submit_permitted=True,
        notes="ATS fictício local para validação E2E em loopback.",
    ),
    "greenhouse": AtsAdapterSpec(
        name="greenhouse",
        domain_patterns=("boards.greenhouse.io", "job-boards.greenhouse.io"),
        automated_submit_permitted=False,
        notes="Regras variam por empregador (reCAPTCHA/perguntas customizadas); parar antes do envio para revisão.",
    ),
    "ashby": AtsAdapterSpec(
        name="ashby",
        domain_patterns=("jobs.ashbyhq.com",),
        automated_submit_permitted=False,
        notes="Preparar rascunho/anexo somente com revisão humana antes do Submit.",
    ),
    "lever": AtsAdapterSpec(
        name="lever",
        domain_patterns=("jobs.lever.co", "jobs.eu.lever.co"),
        automated_submit_permitted=False,
        notes="Preparação determinística sem envio automático por padrão.",
    ),
    "smartrecruiters": AtsAdapterSpec(
        name="smartrecruiters",
        domain_patterns=("jobs.smartrecruiters.com",),
        automated_submit_permitted=False,
        notes="Requer confirmação humana explícita no portal.",
    ),
}


def validate_visual_action(action: str) -> AllowedVisualAction:
    """Enforce strict allowlist on optional fallback planner actions (never shell/eval/arbitrary URL)."""
    norm = action.strip().lower()
    if norm not in ALLOWED_VISUAL_ACTIONS:
        raise ValueError(f"Ação visual não permitida pela allowlist: {action!r}")
    return norm  # type: ignore[return-value]
