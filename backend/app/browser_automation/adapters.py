"""Portal adapters and safe origin classification for CareerPilot Phase 3 copilot."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True, slots=True)
class AtsAdapterSpec:
    """Bounded, step-oriented configuration for a portal or generic external ATS."""

    name: str
    domain_patterns: tuple[str, ...]
    container_selectors: tuple[str, ...]
    apply_cta_patterns: tuple[str, ...]
    profile_surface_hints: tuple[str, ...]
    custom_widget_selectors: tuple[str, ...]
    notes: str


KNOWN_PORTAL_OR_ATS_DOMAINS: tuple[str, ...] = (
    "linkedin.com",
    "gupy.io",
    "gupy.com.br",
    "infojobs.com.br",
    "greenhouse.io",
    "lever.co",
    "myworkdayjobs.com",
    "workday.com",
    "ashbyhq.com",
    "smartrecruiters.com",
    "workable.com",
    "bamboohr.com",
    "solides.com.br",
    "kenoby.com",
    "inhire.app",
    "recruitee.com",
    "teamtailor.com",
)

ATS_ADAPTERS: dict[str, AtsAdapterSpec] = {
    "linkedin": AtsAdapterSpec(
        name="linkedin",
        domain_patterns=("linkedin.com",),
        container_selectors=(
            "[role='dialog'].jobs-easy-apply-modal",
            "div[data-test-modal-id='easy-apply-modal']",
            ".jobs-easy-apply-modal",
            ".artdeco-modal:has(input, textarea, select)",
            "[role='dialog']:has(form)",
            "[role='dialog']:has(input, textarea, select)",
            "form.jobs-easy-apply-form",
        ),
        apply_cta_patterns=(
            r"easy apply",
            r"candidatura simplificada",
            r"candidatar-se",
            r"\bapply\b",
        ),
        profile_surface_hints=(r"/in/[^/]+/edit", r"/mypreferences"),
        custom_widget_selectors=(
            "[role='combobox']",
            "button[aria-haspopup='listbox']",
            "[data-test-single-typeahead-entity-form-component]",
        ),
        notes="Modal LinkedIn Easy Apply; usuário abre a janela, avança etapas e envia no Chrome.",
    ),
    "gupy": AtsAdapterSpec(
        name="gupy",
        domain_patterns=("gupy.io", "gupy.com.br"),
        container_selectors=(
            "[data-testid='application-form'] form",
            "[data-testid*='application'] form",
            "[role='dialog']:has(form)",
            "[role='dialog']:has(input, textarea, select)",
            "main form",
            "form",
            "main:has(textarea, input:not([type='hidden']):not([type='search']))",
        ),
        apply_cta_patterns=(
            r"candidatar-se",
            r"candidatar",
            r"iniciar candidatura",
            r"aplicar",
        ),
        profile_surface_hints=(
            r"/candidates/profile",
            r"/meu-perfil",
            r"curr[ií]culo gupy",
            r"perfil global",
        ),
        custom_widget_selectors=(
            "[role='combobox']",
            "[aria-haspopup='listbox']",
            "[class*='select__control']",
        ),
        notes=(
            "Gupy fluxo por etapas; conta e perfil global da Gupy são separados da candidatura da vaga."
        ),
    ),
    "infojobs": AtsAdapterSpec(
        name="infojobs",
        domain_patterns=("infojobs.com.br",),
        container_selectors=(
            "form#apply-form",
            "form[data-testid*='apply']",
            "[role='dialog']:has(form)",
            "[role='dialog']:has(input, textarea, select)",
            "main form",
            "form",
            "main:has(textarea, input:not([type='hidden']):not([type='search']))",
            "body:has(textarea)",
        ),
        apply_cta_patterns=(
            r"candidatar-me",
            r"candidatar-se",
            r"enviar curr[ií]culo",
            r"\baplicar\b",
        ),
        profile_surface_hints=(r"/candidate/cv", r"/curriculum", r"meu curr[ií]culo"),
        custom_widget_selectors=("[role='combobox']", "[aria-haspopup='listbox']"),
        notes="InfoJobs candidatura por etapas; usuário avança e finaliza no Chrome.",
    ),
    "greenhouse": AtsAdapterSpec(
        name="greenhouse",
        domain_patterns=("greenhouse.io",),
        container_selectors=(
            "form#application_form",
            "form[action*='applications']",
            "main form",
            "form",
        ),
        apply_cta_patterns=(r"apply for this job", r"apply now", r"candidatar"),
        profile_surface_hints=(),
        custom_widget_selectors=("[role='combobox']", ".select2-container"),
        notes="ATS externo Greenhouse; usuário revisa e envia no Chrome.",
    ),
    "lever": AtsAdapterSpec(
        name="lever",
        domain_patterns=("lever.co",),
        container_selectors=(
            "form#application-form",
            "form.application-form",
            "main form",
            "form",
        ),
        apply_cta_patterns=(r"apply for this job", r"submit your application"),
        profile_surface_hints=(),
        custom_widget_selectors=("[role='combobox']",),
        notes="ATS externo Lever; usuário revisa e envia no Chrome.",
    ),
}

GENERIC_ADAPTER = AtsAdapterSpec(
    name="generic",
    domain_patterns=(),
    container_selectors=(
        "[role='dialog']:has(form)",
        "[role='dialog']:has(input, textarea, select)",
        "main form",
        "form[action*='apply']",
        "form[action*='candidat']",
        "form[id*='apply']",
        "form[class*='apply']",
        "form",
        "main:has(textarea, input:not([type='hidden']):not([type='search']))",
    ),
    apply_cta_patterns=(
        r"candidatar",
        r"apply now",
        r"apply for this job",
        r"enviar candidatura",
    ),
    profile_surface_hints=(),
    custom_widget_selectors=("[role='combobox']", "[aria-haspopup='listbox']"),
    notes="ATS externo genérico com contêiner único de candidatura; usuário revisa e envia no Chrome.",
)


def _host_matches_domain(hostname: str, domain: str) -> bool:
    host = hostname.lower().rstrip(".")
    target = domain.lower().rstrip(".")
    return host == target or host.endswith(f".{target}")


def detect_portal(url: str) -> str:
    """Identify supported portal adapter from URL hostname, defaulting to `generic`."""
    parsed = urlparse((url or "").strip())
    host = (parsed.hostname or "").lower()
    if not host:
        return "generic"
    for name, spec in ATS_ADAPTERS.items():
        if any(_host_matches_domain(host, pattern) for pattern in spec.domain_patterns):
            return name
    return "generic"


def get_adapter(portal: str) -> AtsAdapterSpec:
    """Return adapter specification by name, defaulting to the generic external ATS adapter."""
    return ATS_ADAPTERS.get((portal or "").lower(), GENERIC_ADAPTER)


def is_approved_ats_origin(origin: str) -> bool:
    """Return True if an origin belongs to a known job portal or external ATS domain."""
    host = (urlparse(origin).hostname or "").lower()
    if not host:
        return False
    return any(_host_matches_domain(host, domain) for domain in KNOWN_PORTAL_OR_ATS_DOMAINS)


def _registrable_suffix(hostname: str) -> str:
    host = hostname.lower().rstrip(".")
    parts = [p for p in host.split(".") if p]
    if len(parts) <= 2:
        return host
    if len(parts) >= 3 and parts[-1] == "br" and parts[-2] in {"com", "org", "net", "gov", "edu"}:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def is_same_site_origin(origin_a: str, origin_b: str) -> bool:
    """Return True when two origins share scheme + registrable domain (and port for loopback)."""
    pa = urlparse(origin_a)
    pb = urlparse(origin_b)
    if pa.scheme.lower() != pb.scheme.lower():
        return False
    ha = (pa.hostname or "").lower()
    hb = (pb.hostname or "").lower()
    if not ha or not hb:
        return False
    if ha == hb and (pa.port or None) == (pb.port or None):
        return True
    if ha in {"localhost", "127.0.0.1", "::1"} or hb in {"localhost", "127.0.0.1", "::1"}:
        return ha == hb and (pa.port or None) == (pb.port or None)
    return _registrable_suffix(ha) == _registrable_suffix(hb)


def is_compatible_application_origin(
    *,
    bound_origin: str,
    current_origin: str,
    job_origin: str | None = None,
) -> bool:
    """Verify an already-bound application tab remained on an approved origin/handoff.

    Allows:
    - Same origin or same registrable portal domain (e.g. `linkedin.com` -> `www.linkedin.com`,
      `acme.gupy.io` -> `portal.gupy.io`).
    - Explicit job origin stored on the vacancy.
    - Transition to a recognized public ATS domain (external Apply handoff).
    Rejects unrelated non-ATS third-party origins after binding.
    """
    if not bound_origin or not current_origin:
        return False
    if is_same_site_origin(bound_origin, current_origin):
        return True
    if job_origin and is_same_site_origin(job_origin, current_origin):
        return True
    return is_approved_ats_origin(current_origin)


def matches_profile_surface_warning(portal: str, url: str, heading: str) -> str | None:
    """Return a user-facing notice when a portal shows global account/profile settings vs job form."""
    spec = get_adapter(portal)
    haystack = f"{url}\n{heading}".lower()
    for pat in spec.profile_surface_hints:
        if re.search(pat, haystack, re.I):
            return (
                f"Esta página do portal ({spec.name}) refere-se ao perfil global da sua conta, "
                "separado do formulário específico desta vaga."
            )
    return None
