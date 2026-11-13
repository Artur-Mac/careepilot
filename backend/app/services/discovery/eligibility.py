"""Geographic scope and Brazil/Recife eligibility classifier for discovered jobs."""

from __future__ import annotations

import re
from typing import Any

_RECIFE_RE = re.compile(
    r"\b(recife|olinda|jaboat[aã]o|camaragibe|paulista|cabo de santo agostinho|"
    r"porto digital|pernambuco|recife\s*[-,/]\s*pe)\b",
    re.IGNORECASE,
)

_WORLDWIDE_RE = re.compile(
    r"\b(worldwide|work from anywhere|anywhere in the world|global remote|remoto global|"
    r"anywhere|qualquer lugar do mundo|todo o mundo)\b",
    re.IGNORECASE,
)

_LATAM_RE = re.compile(
    r"\b(latam|latin america|am[eé]rica latina|south america|am[eé]rica do sul)\b",
    re.IGNORECASE,
)

_BRAZIL_RE = re.compile(r"\b(brasil|brazil)\b", re.IGNORECASE)

_EXPLICIT_FOREIGN_ONLY_RE = re.compile(
    r"\b("
    r"us[\s-]?only|u\.s\.[\s-]?only|united\s+states\s+only|usa\s+only|"
    r"canada\s+only|uk\s+only|united\s+kingdom\s+only|eu[\s-]?only|europe\s+only|eea\s+only|"
    r"apac\s+only|emea\s+only|north\s+america\s+only|"
    r"only\s+in\s+the\s+(us|u\.s\.|united\s+states|uk|eu|europe|canada)|"
    r"within\s+the\s+(us|u\.s\.|united\s+states|eu|uk)|"
    r"must\s+(?:be\s+located|reside|live|work)\s+(?:within\s+|in\s+)(?:the\s+)?"
    r"(us|u\.s\.|united\s+states|usa|eu|europe|uk|united\s+kingdom|canada|germany)|"
    r"authorized\s+to\s+work\s+in\s+the\s+(us|u\.s\.|united\s+states)|"
    r"residents\s+of\s+(?:the\s+)?(us|u\.s\.|united\s+states|uk|canada|eu|europe)|"
    r"apenas\s+(?:para\s+)?(?:eua|estados\s+unidos|europa|reino\s+unido|canad[aá]|portugal|alemanha)|"
    r"somente\s+(?:para\s+)?(?:eua|estados\s+unidos|europa|reino\s+unido|canad[aá])"
    r")\b",
    re.IGNORECASE,
)

_FOREIGN_COUNTRY_LOCATION_RE = re.compile(
    r"\b("
    r"united\s+states|usa|u\.s\.|canada|united\s+kingdom|uk|england|germany|deutschland|"
    r"france|spain|portugal|ireland|netherlands|poland|india|australia|singapore|"
    r"new\s+york|san\s+francisco|austin,\s*tx|seattle,\s*wa|boston,\s*ma|chicago,\s*il|"
    r"london|berlin|munich|dublin|toronto|vancouver|madrid|lisbon|lisboa|paris"
    r")\b",
    re.IGNORECASE,
)


def classify_job_eligibility(
    *,
    title: str,
    location: str | None,
    workplace_type: str,
    description: str,
) -> dict[str, Any]:
    """Classify geographic scope and Brazil/Recife eligibility conservatively.

    Returns dict with keys:
    - remote_scope: recife_pe | recife_hybrid | brazil | latam | global | other_city |
                    restricted_region | restricted_country | unspecified
    - brazil_eligible: eligible | restricted | unspecified
    - reason: concise PT-BR human-readable explanation or None
    """
    loc = (location or "").strip()
    header_text = f"{loc} {title}".strip()
    full_text = f"{title} {loc} {description}".strip()
    wp = (workplace_type or "unknown").lower()

    if _RECIFE_RE.search(loc) or (wp != "remote" and _RECIFE_RE.search(title)):
        return {
            "remote_scope": "recife_hybrid" if wp == "hybrid" else "recife_pe",
            "brazil_eligible": "eligible",
            "reason": "Localização compatível com Região Metropolitana do Recife, PE.",
        }

    # Explicit non-Recife onsite or hybrid job is outside CareerPilot's Recife/Remote scope
    if (
        wp in {"onsite", "hybrid"}
        and loc
        and not re.fullmatch(r"(?i)(brasil|brazil|remoto|remote)", loc)
    ):
        return {
            "remote_scope": "other_city",
            "brazil_eligible": "restricted",
            "reason": f"Vaga {wp} localizada fora de Recife/PE ({loc[:80]}).",
        }

    has_brazil = bool(_BRAZIL_RE.search(full_text))
    has_latam = bool(_LATAM_RE.search(full_text))
    has_worldwide = bool(_WORLDWIDE_RE.search(full_text))

    if _EXPLICIT_FOREIGN_ONLY_RE.search(full_text):
        return {
            "remote_scope": "restricted_region",
            "brazil_eligible": "restricted",
            "reason": "Vaga remota restrita a outro país/região (ex.: apenas EUA/Europa).",
        }

    if has_worldwide and (wp == "remote" or "remote" in header_text.lower() or "remoto" in header_text.lower()):
        return {
            "remote_scope": "global",
            "brazil_eligible": "eligible",
            "reason": "Vaga remota com escopo mundial/work from anywhere.",
        }

    if has_latam and (wp == "remote" or "remote" in header_text.lower() or "remoto" in header_text.lower()):
        return {
            "remote_scope": "latam",
            "brazil_eligible": "eligible",
            "reason": "Vaga remota elegível para América Latina (LATAM).",
        }

    if has_brazil and (wp == "remote" or "remoto" in header_text.lower() or "remote" in header_text.lower() or _BRAZIL_RE.search(loc)):
        return {
            "remote_scope": "brazil",
            "brazil_eligible": "eligible",
            "reason": "Vaga remota aberta para residentes no Brasil.",
        }

    if loc and _FOREIGN_COUNTRY_LOCATION_RE.search(loc) and not (has_brazil or has_latam or has_worldwide):
        return {
            "remote_scope": "restricted_country",
            "brazil_eligible": "restricted",
            "reason": f"Localização externa ({loc[:80]}) sem indicação de elegibilidade Brasil/LATAM.",
        }

    if wp == "remote" or "remote" in header_text.lower() or "remoto" in header_text.lower():
        return {
            "remote_scope": "unspecified",
            "brazil_eligible": "unspecified",
            "reason": "Vaga remota sem restrição regional explícita; confirme elegibilidade no portal.",
        }

    if _RECIFE_RE.search(full_text):
        return {
            "remote_scope": "recife_pe",
            "brazil_eligible": "eligible",
            "reason": "Menção direta a Recife/PE na descrição da vaga.",
        }

    return {
        "remote_scope": "unspecified",
        "brazil_eligible": "unspecified",
        "reason": None,
    }
