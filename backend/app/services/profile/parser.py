"""Deterministic Phase 1 state-machine resume parser and repair-diff builder."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from backend.app.core.time import normalize_nfc
from backend.app.schemas.profile import (
    MAX_EDUCATION_ITEMS,
    MAX_EXPERIENCE_ITEMS,
    MAX_LANGUAGE_ITEMS,
    MAX_PROJECT_ITEMS,
    MAX_SKILL_ITEMS,
    EducationEntry,
    EvidenceRef,
    ImportRepairDiff,
    ImportSectionCounts,
    ImportWarning,
    LanguageEntry,
    MasterProfileDocumentV2,
    RoutedBullet,
    RoutedExperienceEntry,
    RoutedProjectEntry,
    SkillEntry,
)

if TYPE_CHECKING:
    from backend.app.services.resume.service import ExtractedBlock

KNOWN_SKILLS: tuple[str, ...] = (
    "python",
    "sql",
    "fastapi",
    "etl",
    "databricks",
    "azure",
    "power bi",
    "pandas",
    "pytest",
    "sqlalchemy",
    "git",
    "docker",
    "machine learning",
    "dbt",
    "spark",
    "airflow",
    "aws",
    "gcp",
    "kubernetes",
    "rust",
    "typescript",
    "javascript",
    "react",
    "postgresql",
)

_CANONICAL_KNOWN_SKILLS: dict[str, str] = {
    "sql": "SQL",
    "etl": "ETL",
    "aws": "AWS",
    "gcp": "GCP",
    "fastapi": "FastAPI",
    "pytest": "pytest",
    "sqlalchemy": "SQLAlchemy",
    "power bi": "Power BI",
    "typescript": "TypeScript",
    "javascript": "JavaScript",
    "postgresql": "PostgreSQL",
}

_GOV_ID_LINE_RE = re.compile(r"\b(cpf|rg\s*:|cnh|passaporte|passport)\b", re.I)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{2,3}\)?[\s.-]?)\d{4,5}[\s.-]?\d{4}")
_URL_RE = re.compile(r"https?://[^\s|,)>\]]+")
_BULLET_PREFIX_RE = re.compile(r"^(?:[-*•●▪◦‣]+|–(?=\s)|—(?=\s)|\+(?=\s)|\d{1,2}[.)](?=\s))\s*(.+)$")
_ROLE_HINT_RE = re.compile(
    r"\b(?:analista|engenheir[oa]|desenvolvedor[oa]|cientista|arquiteto|arquiteta|"
    r"estagi[aá]ri[oa]|programador[oa]|consultor[ao]|engineer|developer|analyst|"
    r"scientist|architect|intern|programmer)\b",
    re.I,
)
_PERIOD_CORE = (
    r"(?:(?:jan|fev|feb|mar|abr|apr|mai|may|jun|jul|ago|aug|set|sep|out|oct|nov|dez|dec)[\s./]*)?"
    r"(?:\d{2}/)?(?:19|20)\d{2}\s*[–\-−—]+\s*"
    r"(?:(?:(?:jan|fev|feb|mar|abr|apr|mai|may|jun|jul|ago|aug|set|sep|out|oct|nov|dez|dec)[\s./]*)?"
    r"(?:\d{2}/)?(?:19|20)\d{2}|atual|presente|present|current)"
    r"(?:\s*\(previsto\))?"
)
_TRAILING_PERIOD_RE = re.compile(rf"\s*(?:\(\s*|\[\s*)?({_PERIOD_CORE})(?:\s*[\)\]])?$", re.I)
_STANDALONE_PERIOD_RE = re.compile(rf"^({_PERIOD_CORE})(?:\s*[|—–\-]\s*(.+))?$", re.I)
_PERIOD_HINT_RE = re.compile(r"\b(?:19\d{2}|20\d{2}|atual|presente|present|current)\b", re.I)
_CONTACT_DOMAIN_RE = re.compile(r"\b(?:linkedin\.com|github\.com|gitlab\.com|portfolio)\b", re.I)
_EDUCATION_HINT_RE = re.compile(
    r"\b(?:bacharel(?:ado)?|licenciatura|tecn[oó]logo|gradua[cç][aã]o|mestrado|doutorado|"
    r"p[oó]s-gradua[cç][aã]o|mba|universidade|faculdade|instituto federal|ufpe|ufrpe|ifpe|"
    r"usp|unicamp|ufrj|ufmg|b\.?sc\.?|m\.?sc\.?|degree|university|college)\b",
    re.I,
)
_INSTITUTION_HINT_RE = re.compile(
    r"\b(?:universidade|faculdade|instituto federal|centro universit[aá]rio|escola|ufpe|ufrpe|ifpe|"
    r"usp|unicamp|ufrj|ufmg|university|college|institute)\b",
    re.I,
)
_DEGREE_HINT_RE = re.compile(
    r"\b(?:bacharel(?:ado)?|licenciatura|tecn[oó]logo|gradua[cç][aã]o|mestrado|doutorado|"
    r"p[oó]s-gradua[cç][aã]o|mba|t[eé]cnico|b\.?sc\.?|m\.?sc\.?|ph\.?d\.?|degree|bachelor|master)\b",
    re.I,
)
_SECTION_ALIASES: dict[str, str] = {
    # Experience
    "experiencia profissional": "experience",
    "experiencias profissionais": "experience",
    "experiencia": "experience",
    "experiencias": "experience",
    "historico profissional": "experience",
    "trajetoria profissional": "experience",
    "work experience": "experience",
    "professional experience": "experience",
    "experience": "experience",
    "employment history": "experience",
    "career history": "experience",
    # Education
    "formacao academica": "education",
    "formacao": "education",
    "educacao": "education",
    "escolaridade": "education",
    "education": "education",
    "academic background": "education",
    "academic history": "education",
    # Projects
    "projetos": "project",
    "projetos tecnicos": "project",
    "projetos relevantes": "project",
    "projetos e premiacoes": "project",
    "projects": "project",
    "technical projects": "project",
    "personal projects": "project",
    "selected projects": "project",
    # Skills
    "competencias": "skills",
    "competencias tecnicas": "skills",
    "habilidades": "skills",
    "habilidades tecnicas": "skills",
    "tecnologias": "skills",
    "conhecimentos tecnicos": "skills",
    "skills": "skills",
    "technical skills": "skills",
    "core competencies": "skills",
    "tech stack": "skills",
    # Languages
    "idiomas": "language",
    "linguas": "language",
    "languages": "language",
    # Summary / General
    "resumo": "general",
    "resumo profissional": "general",
    "perfil profissional": "general",
    "sobre": "general",
    "sobre mim": "general",
    "objetivo": "general",
    "summary": "general",
    "professional summary": "general",
    "about": "general",
    "about me": "general",
    "objective": "general",
}


def fold_ascii(text: str) -> str:
    """Lowercase string with combining accents stripped for accent-folded matching."""
    decomposed = unicodedata_nfkd(text)
    return "".join(ch for ch in decomposed if not _is_combining(ch)).lower()


def unicodedata_nfkd(text: str) -> str:
    import unicodedata

    return unicodedata.normalize("NFKD", text)


def _is_combining(ch: str) -> bool:
    import unicodedata

    return bool(unicodedata.combining(ch))


def normalize_match_key(text: str) -> str:
    """Accent-folded whitespace-collapsed comparison key (separate from display text)."""
    return re.sub(r"\s+", " ", fold_ascii(text)).strip()


def detect_section_header(stripped: str, *, heading_level: int | None = None) -> str | None:
    """Identify standalone bilingual section headings via anchored alias table."""
    has_md_prefix = stripped.startswith("#")
    raw_hdr = stripped.lstrip("#").strip().rstrip(":").strip()
    if not raw_hdr or len(raw_hdr) > 85:
        return None
    if raw_hdr.endswith(".") and not has_md_prefix and heading_level is None:
        return None
    words = raw_hdr.split()
    is_upper_banner = raw_hdr == raw_hdr.upper() and any(c.isalpha() for c in raw_hdr)
    if not has_md_prefix and heading_level is None and not is_upper_banner and len(words) > 6 and ":" not in stripped:
        return None

    folded = re.sub(r"[^a-z0-9]+", " ", fold_ascii(raw_hdr)).strip()
    if folded in _SECTION_ALIASES:
        return _SECTION_ALIASES[folded]

    compact = folded.replace(" ", "")
    if "experienciaprofissional" in compact or "historicoprofissional" in compact or "workexperience" in compact:
        return "experience"
    if "formacaoacademica" in compact or "academicbackground" in compact:
        return "education"
    if "projetos" in compact and (
        "engenharia" in compact or "premiacoes" in compact or "relevantes" in compact
    ):
        return "project"
    if "habilidades" in compact or "competencias" in compact or "technicalskills" in compact:
        return "skills"
    if compact in {"idiomas", "languages"}:
        return "language"
    return None


def is_contact_or_noise_line(
    stripped: str,
    candidate_name: str | None,
    *,
    in_header_section: bool = True,
) -> bool:
    """Return True when a line is contact metadata, ID PII, page number, or repeated name banner."""
    if not stripped or _GOV_ID_LINE_RE.search(stripped):
        return True
    if _EMAIL_RE.search(stripped) or _PHONE_RE.search(stripped):
        return True
    if "|" in stripped and (_CONTACT_DOMAIN_RE.search(stripped) or len(stripped.split("|")) >= 3):
        return True
    if re.match(r"^(?:p[aá]gina|page)\s+\d+(?:\s*(?:de|of|/)\s*\d+)?$", stripped, re.I):
        return True
    if re.match(r"^\d+\s*/\s*\d+$", stripped):
        return True
    if candidate_name:
        line_compact = re.sub(r"\s+", "", fold_ascii(stripped.lstrip("# ").strip()))
        name_compact = re.sub(r"\s+", "", fold_ascii(candidate_name))
        if name_compact and line_compact == name_compact:
            return True
    if in_header_section:
        return bool(
            stripped == stripped.upper()
            and any(c.isalpha() for c in stripped)
            and len(stripped.split()) <= 4
            and not _PERIOD_HINT_RE.search(stripped)
        )
    return False


def _split_top_level_once(text: str, sep: str) -> tuple[str, str] | None:
    """Split `text` on the first `sep` occurring outside parentheses or brackets."""
    depth = 0
    sep_len = len(sep)
    idx = 0
    limit = len(text) - sep_len
    while idx <= limit:
        ch = text[idx]
        if ch in "([":
            depth += 1
        elif ch in ")]" and depth > 0:
            depth -= 1
        elif depth == 0 and text[idx : idx + sep_len] == sep:
            return text[:idx].strip(), text[idx + sep_len :].strip()
        idx += 1
    return None


def split_entry_header(clean_line: str) -> tuple[str | None, str | None, str | None, str]:
    """Parse optional `(company, role, period, base_text)` from a structured non-bullet header line."""
    work = clean_line.strip()
    period: str | None = None
    m_period = _TRAILING_PERIOD_RE.search(work)
    if m_period:
        period = m_period.group(1).strip()
        work = work[: m_period.start()].strip(" -–—|,")

    company: str | None = None
    role: str | None = None
    for sep in (" — ", " – ", " | ", " - "):
        parts = _split_top_level_once(work, sep)
        if parts is not None:
            left, right = parts
            if left and right:
                left_is_role = bool(_ROLE_HINT_RE.search(left))
                right_is_role = bool(_ROLE_HINT_RE.search(right))
                if left_is_role and not right_is_role:
                    company = normalize_nfc(right[:120])
                    role = normalize_nfc(left[:120])
                else:
                    company = normalize_nfc(left[:120])
                    role = normalize_nfc(right[:120])
                break
    if role is None and company is None and work and len(work) < 100 and not work.endswith("."):
        role = normalize_nfc(work[:120])
    return company, role, period, work or clean_line


def is_entry_boundary_header(clean_line: str, *, heading_level: int | None = None) -> bool:
    """Return True when a non-bullet line looks like the start of a new experience or project record."""
    if heading_level is not None and heading_level >= 2:
        return True
    if clean_line.startswith("#") or " — " in clean_line or " – " in clean_line or " | " in clean_line:
        return True
    if not clean_line.endswith(".") and len(clean_line) <= 120 and _split_top_level_once(clean_line, " - ") is not None:
        return True
    if _TRAILING_PERIOD_RE.search(clean_line):
        return True
    return bool(re.match(r"^\d+[.)]\s+\S", clean_line))


def should_merge_wrapped_line(
    clean_line: str,
    prev_text: str,
    *,
    saw_blank_line: bool,
    across_page_break: bool = False,
    heading_level: int | None = None,
) -> bool:
    """Detect soft wraps (and evidence-backed page-break continuations) belonging to an open bullet."""
    if not prev_text or not clean_line:
        return False
    if is_entry_boundary_header(clean_line, heading_level=heading_level):
        return False
    if saw_blank_line and not across_page_break:
        return False
    if clean_line[0].islower():
        return True
    prev_tail = prev_text.rstrip()
    return (
        len(prev_tail) < 420
        and not prev_tail.endswith((".", "!", "?", ";"))
        and len(clean_line.split()) >= 2
    )


def _split_skills_line(raw_line: str) -> tuple[str | None, list[str]]:
    """Split skills list preserving C++, C#, .NET and dropping empty/punctuation-only tokens."""
    work = raw_line.strip()
    # Strip outer bullet symbols safely without eating `.` in `.NET`!
    m_bul = _BULLET_PREFIX_RE.match(work)
    work = m_bul.group(1).strip() if m_bul else re.sub(r"^[-*•●▪◦‣]+\s*", "", work)

    category: str | None = None
    if ":" in work:
        left, right = work.split(":", 1)
        if left.strip() and len(left.strip()) <= 45 and not re.search(r"[,;|]", left):
            category = normalize_nfc(left.strip())
            work = right.strip()

    raw_tokens = re.split(r"[,;|\n]+|\s+/\s+", work)
    cleaned: list[str] = []
    for raw_tok in raw_tokens:
        tok = re.sub(r"^[-*•●▪◦‣\s]+|[-*•●▪◦‣\s]+$", "", raw_tok).strip()
        if not tok or not any(ch.isalnum() for ch in tok):
            continue
        cleaned.append(normalize_nfc(tok))
    return category, cleaned


def _parse_language_items(raw_line: str) -> list[tuple[str, str | None]]:
    """Parse one or more language items splitting name and explicit proficiency."""
    work = raw_line.strip()
    m_bul = _BULLET_PREFIX_RE.match(work)
    if m_bul:
        work = m_bul.group(1).strip()

    chunks = [c.strip() for c in re.split(r"[,;\n]+", work) if c.strip()]
    if len(chunks) == 1 and " | " in chunks[0]:
        parts = [p.strip() for p in chunks[0].split(" | ") if p.strip()]
        # Split multiple `Pt — Nativo | En — B2` chunks while keeping single `Inglês | B2`.
        if len(parts) >= 2 and all(re.search(r"[:—–-]|\(", p) for p in parts):
            chunks = parts

    parsed: list[tuple[str, str | None]] = []
    paren_re = re.compile(r"^(.+?)\s*\(([^()]{2,50})\)$")
    for chunk in chunks:
        item_txt = re.sub(r"^[-*•●▪◦‣\s]+", "", chunk).strip()
        if not item_txt or not any(ch.isalnum() for ch in item_txt):
            continue
        m_paren = paren_re.match(item_txt)
        if m_paren:
            name = normalize_nfc(m_paren.group(1).strip())
            prof = normalize_nfc(m_paren.group(2).strip()) or None
            if name and len(name) <= 120:
                parsed.append((name, prof))
            continue
        matched_sep = False
        for sep in (":", " — ", " – ", " - ", " | "):
            if sep in item_txt:
                left, right = (p.strip() for p in item_txt.split(sep, 1))
                if left and any(ch.isalnum() for ch in left):
                    parsed.append((normalize_nfc(left[:120]), normalize_nfc(right) or None))
                    matched_sep = True
                    break
        if not matched_sep and any(ch.isalnum() for ch in item_txt) and len(item_txt) <= 120:
            parsed.append((normalize_nfc(item_txt), None))
    return parsed


def _parse_education_fields(clean_line: str) -> tuple[str | None, str | None, str | None, str]:
    """Extract optional (institution, degree, period, remaining_details) from an education line."""
    work = clean_line.strip()
    period: str | None = None
    m_period = _TRAILING_PERIOD_RE.search(work)
    if m_period:
        period = m_period.group(1).strip()
        work = work[: m_period.start()].strip(" -–—|,")

    institution: str | None = None
    degree: str | None = None

    m_lbl = re.match(r"^(institui[cç][aã]o|universidade|faculdade)\s*:\s*(.+)$", work, re.I)
    if m_lbl:
        return normalize_nfc(m_lbl.group(2).strip()[:160]), None, period, ""
    m_deg_lbl = re.match(r"^(curso|grau|forma[cç][aã]o|degree)\s*:\s*(.+)$", work, re.I)
    if m_deg_lbl:
        return None, normalize_nfc(m_deg_lbl.group(2).strip()[:160]), period, ""

    for sep in (" — ", " – ", " | ", " - "):
        parts = _split_top_level_once(work, sep)
        if parts is not None:
            left, right = parts
            if left and right:
                if _INSTITUTION_HINT_RE.search(right) and not _INSTITUTION_HINT_RE.search(left):
                    degree = normalize_nfc(left[:160])
                    institution = normalize_nfc(right[:160])
                elif (_DEGREE_HINT_RE.search(right) and not _DEGREE_HINT_RE.search(left)) or _INSTITUTION_HINT_RE.search(left):
                    institution = normalize_nfc(left[:160])
                    degree = normalize_nfc(right[:160])
                else:
                    degree = normalize_nfc(left[:160])
                    institution = normalize_nfc(right[:160])
                return institution, degree, period, clean_line

    if _INSTITUTION_HINT_RE.search(work) and not _DEGREE_HINT_RE.search(work):
        institution = normalize_nfc(work[:160])
    elif _DEGREE_HINT_RE.search(work) or _EDUCATION_HINT_RE.search(work):
        degree = normalize_nfc(work[:160])
    return institution, degree, period, clean_line


class _DeterministicIdFactory:
    """Generates collision-free deterministic record IDs anchored to source digest + key."""

    def __init__(self, source_sha256: str) -> None:
        self._sha = source_sha256
        self._counts: dict[str, int] = {}

    def make(self, prefix: str, seed_key: str) -> str:
        digest = hashlib.sha256(f"{self._sha}:{prefix}:{seed_key}".encode()).hexdigest()[:12]
        base = f"{prefix}-{digest}"
        seq = self._counts.get(base, 0) + 1
        self._counts[base] = seq
        if seq == 1:
            return base
        return f"{prefix}-{hashlib.sha256(f'{base}:{seq}'.encode()).hexdigest()[:12]}"


@dataclass(slots=True)
class ParsedResumeResult:
    contact_name: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    linkedin: str | None = None
    github: str | None = None
    portfolio: str | None = None
    experience: list[RoutedExperienceEntry] = field(default_factory=list)
    education: list[EducationEntry] = field(default_factory=list)
    projects: list[RoutedProjectEntry] = field(default_factory=list)
    skills: list[SkillEntry] = field(default_factory=list)
    languages: list[LanguageEntry] = field(default_factory=list)
    warnings: list[ImportWarning] = field(default_factory=list)
    orphaned_blocks: list[dict[str, Any]] = field(default_factory=list)
    excluded_blocks: int = 0
    total_bullets: int = 0


def parse_extracted_blocks(
    blocks: list[ExtractedBlock],
    *,
    filename: str,
    doc_sha: str,
    full_text: str,
    initial_warnings: list[ImportWarning] | None = None,
) -> ParsedResumeResult:
    """Parse ordered structural blocks with section-and-record state machine."""
    ids = _DeterministicIdFactory(doc_sha)
    out = ParsedResumeResult(warnings=list(initial_warnings or []))

    # Extract header contact hints deterministically without touching existing user values at merge time
    non_empty_texts = [b.text for b in blocks if b.text.strip()]
    for b in blocks[:8]:
        raw = b.text.strip()
        if not raw or b.list_marker is not None or _BULLET_PREFIX_RE.match(raw):
            continue
        if detect_section_header(raw, heading_level=b.heading_level) is not None:
            break
        cand_name = raw.lstrip("#").strip()
        if (
            2 <= len(cand_name) <= 90
            and "@" not in cand_name
            and "http" not in cand_name.lower()
            and not _GOV_ID_LINE_RE.search(cand_name)
        ):
            out.contact_name = normalize_nfc(cand_name)
            break

    email_m = _EMAIL_RE.search(full_text)
    if email_m:
        out.contact_email = email_m.group(0)

    for ln in non_empty_texts[:12]:
        if _GOV_ID_LINE_RE.search(ln):
            continue
        ph_m = _PHONE_RE.search(ln)
        if ph_m:
            out.contact_phone = ph_m.group(0).strip()
            break

    for url in _URL_RE.findall(full_text):
        clean_u = url.rstrip(".,;")
        low_u = clean_u.lower()
        if "linkedin.com" in low_u and not out.linkedin:
            out.linkedin = clean_u
        elif "github.com" in low_u and not out.github:
            out.github = clean_u
        elif (
            any(k in low_u for k in ("portfolio", "vercel.app", "netlify.app", "github.io"))
            and not out.portfolio
        ):
            out.portfolio = clean_u

    section_mode = "general"
    active_exp: RoutedExperienceEntry | None = None
    active_exp_accepts_followup = False
    active_exp_header_text = ""

    active_proj: RoutedProjectEntry | None = None
    active_proj_accepts_followup = False
    active_proj_header_text = ""

    active_edu: EducationEntry | None = None
    pending_edu_candidate: tuple[str, ExtractedBlock] | None = None

    pending_header: tuple[str, ExtractedBlock] | None = None
    prev_page: int | None = None
    source_tag = f"import:{filename}"

    seen_skill_keys: set[str] = set()
    seen_lang_keys: set[str] = set()
    seen_exp_keys: set[str] = set()
    seen_proj_keys: set[str] = set()
    seen_edu_keys: set[str] = set()

    def _ev_for(block: ExtractedBlock, snippet: str) -> EvidenceRef:
        return EvidenceRef(
            document_name=filename,
            section_or_page=block.section_label,
            snippet=snippet[:240],
            source_sha256=doc_sha,
        )

    def _add_page_evidence(ev_list: list[EvidenceRef], block: ExtractedBlock, snippet: str) -> None:
        if len(ev_list) >= 10:
            return
        if any(e.section_or_page == block.section_label for e in ev_list):
            return
        ev_list.append(_ev_for(block, snippet))

    def _flush_pending_header_as_unresolved() -> None:
        nonlocal pending_header
        if pending_header is None:
            return
        text_hdr, blk_hdr = pending_header
        pending_header = None
        out.excluded_blocks += 1
        out.orphaned_blocks.append(
            {
                "code": "uncorroborated_block",
                "section": section_mode,
                "line": blk_hdr.ordinal,
                "page": blk_hdr.page,
                "snippet": text_hdr[:140],
            }
        )
        out.warnings.append(
            ImportWarning(
                code="unresolved_block",
                message=f"Bloco sem cabeçalho ou seção conclusiva preservado para revisão: {text_hdr[:80]}",
                section=section_mode,
                line=blk_hdr.ordinal,
                page=blk_hdr.page,
            )
        )

    def _flush_edu_candidate() -> None:
        nonlocal pending_edu_candidate, active_edu
        if pending_edu_candidate is None:
            return
        cand_txt, cand_blk = pending_edu_candidate
        pending_edu_candidate = None
        if active_edu is not None:
            active_edu.details = f"{active_edu.details} {cand_txt}".strip()
            return
        out.excluded_blocks += 1
        out.warnings.append(
            ImportWarning(
                code="unresolved_education_block",
                message=f"Formação sem instituição ou curso identificado: {cand_txt[:80]}",
                section="education",
                line=cand_blk.ordinal,
                page=cand_blk.page,
            )
        )

    def _open_experience(clean_line: str, block: ExtractedBlock, *, pre_company: str | None = None) -> None:
        nonlocal active_exp, active_exp_accepts_followup, active_exp_header_text
        company, role, period, base_text = split_entry_header(clean_line)
        if pre_company and company is None:
            company = normalize_nfc(pre_company[:120])
        seed = f"{company or ''}|{role or ''}|{period or ''}|{base_text}"
        exp_key = normalize_match_key(seed)
        if exp_key in seen_exp_keys or len(out.experience) >= MAX_EXPERIENCE_ITEMS:
            if len(out.experience) >= MAX_EXPERIENCE_ITEMS:
                out.excluded_blocks += 1
                out.warnings.append(
                    ImportWarning(
                        code="experience_limit_reached",
                        message="Limite máximo de experiências importadas atingido.",
                        section="experience",
                        line=block.ordinal,
                        page=block.page,
                    )
                )
            active_exp = None
            return
        active_exp = RoutedExperienceEntry(
            id=ids.make("exp", seed),
            company=company,
            role=role,
            period=period,
            details=clean_line,
            bullets=[],
            source=source_tag,
            evidence=[_ev_for(block, clean_line)],
        )
        out.experience.append(active_exp)
        seen_exp_keys.add(exp_key)
        active_exp_header_text = clean_line
        active_exp_accepts_followup = (
            is_entry_boundary_header(clean_line, heading_level=block.heading_level)
            or period is not None
            or company is not None
        )

    def _open_project(clean_line: str, block: ExtractedBlock) -> None:
        nonlocal active_proj, active_proj_accepts_followup, active_proj_header_text
        base_work = _TRAILING_PERIOD_RE.sub("", clean_line).strip() or clean_line
        proj_name = normalize_nfc(
            base_work.split(":", 1)[0].split(" — ", 1)[0].split(" – ", 1)[0].strip()[:120] or "Projeto"
        )
        proj_key = normalize_match_key(clean_line)
        if proj_key in seen_proj_keys or len(out.projects) >= MAX_PROJECT_ITEMS:
            if len(out.projects) >= MAX_PROJECT_ITEMS:
                out.excluded_blocks += 1
            active_proj = None
            return
        active_proj = RoutedProjectEntry(
            id=ids.make("prj", clean_line),
            name=proj_name,
            details=clean_line,
            bullets=[],
            source=source_tag,
            evidence=[_ev_for(block, clean_line)],
        )
        out.projects.append(active_proj)
        seen_proj_keys.add(proj_key)
        active_proj_header_text = clean_line
        active_proj_accepts_followup = (
            is_entry_boundary_header(clean_line, heading_level=block.heading_level)
            or len(clean_line) <= 100
        )

    def _append_bullet_to_active(
        bullets: list[RoutedBullet],
        text: str,
        *,
        max_bullets: int,
        prefix: str,
        parent_id: str,
        block: ExtractedBlock,
        section: str,
    ) -> bool:
        norm = normalize_nfc(text.strip())
        if not norm:
            return False
        if len(bullets) >= max_bullets:
            out.excluded_blocks += 1
            out.warnings.append(
                ImportWarning(
                    code="bullet_limit_reached",
                    message=f"Limite de marcadores atingido em registro de {section}; item excedente ignorado.",
                    section=section,
                    line=block.ordinal,
                    page=block.page,
                )
            )
            return False
        norm_key = normalize_match_key(norm)
        if any(normalize_match_key(b.text) == norm_key for b in bullets):
            return False
        bullets.append(
            RoutedBullet(
                id=ids.make(prefix, f"{parent_id}:{len(bullets)}:{norm}"),
                text=norm[:580],
                evidence=[_ev_for(block, norm)],
            )
        )
        out.total_bullets += 1
        return True

    for block in blocks:
        stripped = block.text.strip()
        if not stripped:
            prev_page = block.page
            continue

        across_page_break = (
            prev_page is not None and block.page is not None and block.page != prev_page
        )
        detected_section = detect_section_header(stripped, heading_level=block.heading_level)
        if detected_section is not None:
            _flush_pending_header_as_unresolved()
            _flush_edu_candidate()
            section_mode = detected_section
            active_exp = None
            active_exp_accepts_followup = False
            active_proj = None
            active_proj_accepts_followup = False
            active_edu = None
            prev_page = block.page
            continue

        if is_contact_or_noise_line(stripped, out.contact_name, in_header_section=(section_mode == "general")):
            prev_page = block.page
            continue

        low = stripped.lower()

        # Section-specific handlers (Skills & Languages)
        if section_mode == "skills":
            category, tokens = _split_skills_line(stripped)
            for tok in tokens:
                sk_key = normalize_match_key(tok)
                if not sk_key or sk_key in seen_skill_keys:
                    continue
                if len(out.skills) >= MAX_SKILL_ITEMS:
                    out.excluded_blocks += 1
                    out.warnings.append(
                        ImportWarning(
                            code="skills_overflow",
                            message="Limite máximo de habilidades importadas atingido.",
                            section="skills",
                            line=block.ordinal,
                            page=block.page,
                        )
                    )
                    break
                out.skills.append(
                    SkillEntry(
                        id=ids.make("sk", sk_key),
                        name=tok[:120],
                        category=category,
                        source=source_tag,
                    )
                )
                seen_skill_keys.add(sk_key)
            prev_page = block.page
            continue

        if section_mode == "language":
            for lang_name, lang_prof in _parse_language_items(stripped):
                lang_key = normalize_match_key(lang_name)
                if not lang_key or lang_key in seen_lang_keys:
                    continue
                if len(out.languages) >= MAX_LANGUAGE_ITEMS:
                    out.excluded_blocks += 1
                    break
                out.languages.append(
                    LanguageEntry(
                        id=ids.make("lang", lang_key),
                        name=lang_name,
                        proficiency=lang_prof,
                        source=source_tag,
                    )
                )
                seen_lang_keys.add(lang_key)
            prev_page = block.page
            continue

        # Classify list markers BEFORE evaluating header candidates in experience/projects
        bullet_match = _BULLET_PREFIX_RE.match(stripped)
        is_bullet = bullet_match is not None or block.list_marker is not None
        if bullet_match is not None:
            clean_line = bullet_match.group(1).strip()
        else:
            clean_line = stripped.lstrip("# ").strip()
        clean_line = normalize_nfc(clean_line)

        if not clean_line or len(clean_line) > 500:
            if len(clean_line) > 500:
                out.excluded_blocks += 1
            prev_page = block.page
            continue

        # Promote a pending structured header only when corroborated (e.g. DOCX heading or separator) before a bullet
        if is_bullet and pending_header is not None:
            hdr_txt, hdr_blk = pending_header
            pending_header = None
            if is_entry_boundary_header(hdr_txt, heading_level=hdr_blk.heading_level):
                if section_mode == "project":
                    _open_project(hdr_txt, hdr_blk)
                elif section_mode == "experience":
                    _open_experience(hdr_txt, hdr_blk)
            else:
                out.excluded_blocks += 1
                out.orphaned_blocks.append(
                    {
                        "code": "uncorroborated_block",
                        "section": section_mode,
                        "line": hdr_blk.ordinal,
                        "page": hdr_blk.page,
                        "snippet": hdr_txt[:140],
                    }
                )

        if is_bullet:
            target_mode = section_mode
            if target_mode == "general":
                if active_proj is not None:
                    target_mode = "project"
                elif active_edu is not None:
                    target_mode = "education"
                elif active_exp is not None:
                    target_mode = "experience"

            if target_mode == "education":
                if active_edu is not None:
                    if clean_line.lower() not in (active_edu.details or "").lower():
                        active_edu.details = f"{active_edu.details}\n- {clean_line}".strip()
                else:
                    out.excluded_blocks += 1
                    out.orphaned_blocks.append(
                        {
                            "code": "orphan_bullet",
                            "section": "education",
                            "line": block.ordinal,
                            "page": block.page,
                            "snippet": clean_line[:140],
                        }
                    )
                    out.warnings.append(
                        ImportWarning(
                            code="orphan_bullet",
                            message=f"Marcador em Formação sem instituição/curso identificado: {clean_line[:80]}",
                            section="education",
                            line=block.ordinal,
                            page=block.page,
                        )
                    )
            elif target_mode == "project":
                if active_proj is not None:
                    _append_bullet_to_active(
                        active_proj.bullets,
                        clean_line,
                        max_bullets=15,
                        prefix="pb",
                        parent_id=active_proj.id,
                        block=block,
                        section="project",
                    )
                    _add_page_evidence(active_proj.evidence, block, clean_line)
                    active_proj_accepts_followup = False
                else:
                    out.excluded_blocks += 1
                    out.orphaned_blocks.append(
                        {
                            "code": "orphan_bullet",
                            "section": "project",
                            "line": block.ordinal,
                            "page": block.page,
                            "snippet": clean_line[:140],
                        }
                    )
                    out.warnings.append(
                        ImportWarning(
                            code="orphan_bullet",
                            message=f"Marcador órfão sem projeto pai identificado: {clean_line[:80]}",
                            section="project",
                            line=block.ordinal,
                            page=block.page,
                        )
                    )
            elif target_mode == "experience":
                if active_exp is not None:
                    _append_bullet_to_active(
                        active_exp.bullets,
                        clean_line,
                        max_bullets=20,
                        prefix="eb",
                        parent_id=active_exp.id,
                        block=block,
                        section="experience",
                    )
                    _add_page_evidence(active_exp.evidence, block, clean_line)
                    active_exp_accepts_followup = False
                else:
                    out.excluded_blocks += 1
                    out.orphaned_blocks.append(
                        {
                            "code": "orphan_bullet",
                            "section": "experience",
                            "line": block.ordinal,
                            "page": block.page,
                            "snippet": clean_line[:140],
                        }
                    )
                    out.warnings.append(
                        ImportWarning(
                            code="orphan_bullet",
                            message=f"Marcador órfão sem experiência pai identificada: {clean_line[:80]}",
                            section="experience",
                            line=block.ordinal,
                            page=block.page,
                        )
                    )
            else:
                out.excluded_blocks += 1
                out.orphaned_blocks.append(
                    {
                        "code": "orphan_bullet",
                        "section": target_mode,
                        "line": block.ordinal,
                        "page": block.page,
                        "snippet": clean_line[:140],
                    }
                )
                out.warnings.append(
                    ImportWarning(
                        code="orphan_bullet",
                        message=f"Marcador sem seção/registro pai identificado: {clean_line[:80]}",
                        section=target_mode,
                        line=block.ordinal,
                        page=block.page,
                    )
                )
            prev_page = block.page
            continue

        # Non-bullet lines
        if section_mode == "education" or (
            section_mode == "general" and _EDUCATION_HINT_RE.search(clean_line)
        ):
            active_exp = None
            active_exp_accepts_followup = False
            active_proj = None
            active_proj_accepts_followup = False
            _flush_pending_header_as_unresolved()

            inst, deg, per, det = _parse_education_fields(clean_line)
            if active_edu is not None and (
                (inst and active_edu.institution is None)
                or (deg and active_edu.degree is None)
                or (per and active_edu.period is None and not inst and not deg)
                or (
                    not inst
                    and not deg
                    and should_merge_wrapped_line(
                        clean_line,
                        active_edu.details or "",
                        saw_blank_line=block.blank_before,
                        across_page_break=across_page_break,
                        heading_level=block.heading_level,
                    )
                )
            ):
                if inst and not active_edu.institution:
                    active_edu.institution = inst
                if deg and not active_edu.degree:
                    active_edu.degree = deg
                if per and not active_edu.period:
                    active_edu.period = per
                if det and det.lower() not in (active_edu.details or "").lower():
                    active_edu.details = f"{active_edu.details}\n{det}".strip()
                _add_page_evidence(active_edu.evidence, block, clean_line)
            elif inst or deg:
                _flush_edu_candidate()
                edu_key = normalize_match_key(f"{inst or ''}|{deg or ''}|{per or ''}")
                if edu_key not in seen_edu_keys and len(out.education) < MAX_EDUCATION_ITEMS:
                    active_edu = EducationEntry(
                        id=ids.make("edu", edu_key),
                        institution=inst,
                        degree=deg,
                        period=per,
                        details=det or clean_line,
                        source=source_tag,
                        evidence=[_ev_for(block, clean_line)],
                    )
                    out.education.append(active_edu)
                    seen_edu_keys.add(edu_key)
            elif active_edu is not None and block.blank_before is False and not clean_line.endswith("."):
                active_edu.details = f"{active_edu.details}\n{clean_line}".strip()
            else:
                if pending_edu_candidate is not None:
                    prev_cand_txt, _ = pending_edu_candidate
                    pending_edu_candidate = None
                    c_inst, c_deg, c_per, _ = _parse_education_fields(f"{prev_cand_txt} — {clean_line}")
                    if c_inst or c_deg:
                        edu_key = normalize_match_key(f"{c_inst or ''}|{c_deg or ''}|{c_per or ''}")
                        if edu_key not in seen_edu_keys and len(out.education) < MAX_EDUCATION_ITEMS:
                            active_edu = EducationEntry(
                                id=ids.make("edu", edu_key),
                                institution=c_inst,
                                degree=c_deg,
                                period=c_per,
                                details=f"{prev_cand_txt}\n{clean_line}",
                                source=source_tag,
                                evidence=[_ev_for(block, clean_line)],
                            )
                            out.education.append(active_edu)
                            seen_edu_keys.add(edu_key)
                            prev_page = block.page
                            continue
                pending_edu_candidate = (clean_line, block)
            prev_page = block.page
            continue

        if section_mode == "project" or (
            section_mode == "general" and any(w in low for w in ("projeto", "project", "repositório"))
        ):
            active_exp = None
            active_exp_accepts_followup = False
            active_edu = None
            _flush_edu_candidate()

            if (
                active_proj is not None
                and active_proj.bullets
                and should_merge_wrapped_line(
                    clean_line,
                    active_proj.bullets[-1].text,
                    saw_blank_line=block.blank_before,
                    across_page_break=across_page_break,
                    heading_level=block.heading_level,
                )
            ):
                active_proj.bullets[-1].text = f"{active_proj.bullets[-1].text} {clean_line}".strip()[:580]
                _add_page_evidence(active_proj.evidence, block, clean_line)
            elif (
                active_proj is not None
                and not active_proj.bullets
                and not block.blank_before
                and active_proj_accepts_followup
                and not is_entry_boundary_header(clean_line, heading_level=block.heading_level)
            ):
                if not active_proj.details or active_proj.details == active_proj_header_text:
                    active_proj.details = clean_line
                else:
                    active_proj.details = f"{active_proj.details} {clean_line}".strip()
                active_proj_accepts_followup = False
                _add_page_evidence(active_proj.evidence, block, clean_line)
            elif is_entry_boundary_header(clean_line, heading_level=block.heading_level) or active_proj is None:
                _flush_pending_header_as_unresolved()
                if is_entry_boundary_header(clean_line, heading_level=block.heading_level) or len(clean_line) <= 100:
                    _open_project(clean_line, block)
                else:
                    pending_header = (clean_line, block)
            elif not is_entry_boundary_header(clean_line, heading_level=block.heading_level) and not block.blank_before and not active_proj.bullets:
                active_proj.details = f"{active_proj.details} {clean_line}".strip()
            else:
                out.excluded_blocks += 1
                out.warnings.append(
                    ImportWarning(
                        code="ambiguous_block",
                        message=f"Linha fora de marcador em Projetos preservada para revisão: {clean_line[:80]}",
                        section="project",
                        line=block.ordinal,
                        page=block.page,
                    )
                )
            prev_page = block.page
            continue

        if section_mode == "experience" or (section_mode == "general" and len(clean_line) >= 12):
            active_proj = None
            active_proj_accepts_followup = False
            active_edu = None
            _flush_edu_candidate()

            # Check pending multiline header corroboration (e.g. Company line -> Role + Period line)
            if pending_header is not None:
                cand_txt, cand_blk = pending_header
                if _STANDALONE_PERIOD_RE.match(clean_line) or _TRAILING_PERIOD_RE.search(clean_line):
                    pending_header = None
                    _open_experience(clean_line, block, pre_company=cand_txt)
                    prev_page = block.page
                    continue
                if is_entry_boundary_header(clean_line, heading_level=block.heading_level):
                    _flush_pending_header_as_unresolved()

            # Enrich period/location onto an open experience whose header omitted dates
            m_std_per = _STANDALONE_PERIOD_RE.match(clean_line)
            if (
                active_exp is not None
                and active_exp.period is None
                and m_std_per is not None
                and not active_exp.bullets
            ):
                active_exp.period = m_std_per.group(1).strip()
                if m_std_per.group(2) and not active_exp.location:
                    active_exp.location = normalize_nfc(m_std_per.group(2).strip()[:120])
                _add_page_evidence(active_exp.evidence, block, clean_line)
                prev_page = block.page
                continue

            if (
                active_exp is not None
                and active_exp.bullets
                and should_merge_wrapped_line(
                    clean_line,
                    active_exp.bullets[-1].text,
                    saw_blank_line=block.blank_before,
                    across_page_break=across_page_break,
                    heading_level=block.heading_level,
                )
            ):
                active_exp.bullets[-1].text = f"{active_exp.bullets[-1].text} {clean_line}".strip()[:580]
                _add_page_evidence(active_exp.evidence, block, clean_line)
            elif (
                active_exp is not None
                and not active_exp.bullets
                and not block.blank_before
                and active_exp_accepts_followup
                and not is_entry_boundary_header(clean_line, heading_level=block.heading_level)
            ):
                if (
                    active_exp.company is None
                    and active_exp.period is not None
                    and len(clean_line) <= 75
                    and not clean_line.endswith(".")
                ):
                    active_exp.company = clean_line[:120]
                    active_exp.details = f"{active_exp_header_text} | {clean_line}".strip()
                else:
                    if not active_exp.details or active_exp.details == active_exp_header_text:
                        active_exp.details = clean_line
                    else:
                        active_exp.details = f"{active_exp.details} {clean_line}".strip()
                    active_exp_accepts_followup = False
                _add_page_evidence(active_exp.evidence, block, clean_line)
            elif is_entry_boundary_header(clean_line, heading_level=block.heading_level):
                _open_experience(clean_line, block)
            elif section_mode == "experience" and active_exp is not None and not active_exp.bullets and len(clean_line) >= 24:
                # Multiline prose before bullets belongs to active experience details
                active_exp.details = f"{active_exp.details} {clean_line}".strip()
            elif section_mode == "experience" and pending_header is None and len(clean_line) <= 90 and not clean_line.endswith("."):
                pending_header = (clean_line, block)
            else:
                if section_mode == "experience":
                    out.excluded_blocks += 1
                    out.warnings.append(
                        ImportWarning(
                            code="ambiguous_block",
                            message=f"Bloco em Experiência sem estrutura conclusiva preservado para revisão: {clean_line[:80]}",
                            section="experience",
                            line=block.ordinal,
                            page=block.page,
                        )
                    )

        prev_page = block.page

    _flush_pending_header_as_unresolved()
    _flush_edu_candidate()

    # Fallback known skills detection across extracted text (without clobbering existing skills)
    text_lower = full_text.lower()
    for skill in KNOWN_SKILLS:
        if re.search(rf"(?<![\w.+#]){re.escape(skill)}(?![\w.+#])", text_lower) and skill not in seen_skill_keys:
            if len(out.skills) >= MAX_SKILL_ITEMS:
                break
            canonical = _CANONICAL_KNOWN_SKILLS.get(skill, skill.title())
            out.skills.append(
                SkillEntry(
                    id=ids.make("sk", skill),
                    name=canonical,
                    source=source_tag,
                )
            )
            seen_skill_keys.add(skill)

    return out


def build_repair_preview(
    current_doc: MasterProfileDocumentV2,
    *,
    document_id: str,
    base_revision: int,
    reparsed: ParsedResumeResult,
    filename: str,
    doc_sha: str,
) -> tuple[MasterProfileDocumentV2, ImportRepairDiff]:
    """Compute a non-destructive repair proposal preserving user-edited claims."""
    updated = current_doc.model_copy(deep=True)
    source_lines_norm = {
        normalize_match_key(re.sub(r"^[-*•●▪◦‣\s]+", "", ln))
        for ln in next(imp.extracted_text for imp in current_doc.imports if imp.id == document_id).splitlines()
        if ln.strip()
    }

    def _is_parser_owned_item(source: str | None, evidence: list[EvidenceRef], texts: list[str]) -> bool:
        from_this_import = source in {f"import:{filename}", f"upload:{filename}"} or any(
            ev.source_sha256 == doc_sha for ev in evidence
        )
        if not from_this_import:
            return False
        if source == "manual":
            return False
        # Preserve if the user edited the entry into text not originating verbatim from this import's source lines
        for txt in texts:
            norm = normalize_match_key(txt)
            if not norm:
                continue
            if norm not in source_lines_norm and not any(norm in src or src in norm for src in source_lines_norm):
                return False
        return True

    preserved_ids: list[str] = []
    id_mapping: dict[str, str] = {}
    grouped_replacements: list[dict[str, Any]] = []
    removed_empty_skills: list[str] = []
    recovered_education: list[dict[str, Any]] = []

    def _entry_text_blobs(item: RoutedExperienceEntry | RoutedProjectEntry) -> list[str]:
        if isinstance(item, RoutedExperienceEntry):
            raw = [item.company or "", item.role or "", item.period or "", item.details or "", *(b.text for b in item.bullets)]
        else:
            raw = [item.name or "", item.details or "", *(b.text for b in item.bullets)]
        return [norm for r in raw if (norm := normalize_match_key(r))]

    def _map_replaced_entries(
        old_items: list[RoutedExperienceEntry] | list[RoutedProjectEntry],
        new_items: list[RoutedExperienceEntry] | list[RoutedProjectEntry],
    ) -> None:
        if not old_items or not new_items:
            return
        new_blobs = [(cand, _entry_text_blobs(cand)) for cand in new_items]
        for old_item in old_items:
            old_blobs = _entry_text_blobs(old_item)
            best_cand = new_items[0]
            best_score = -1
            if old_blobs:
                old_tokens = set(" ".join(old_blobs).split())
                for cand, cand_blobs in new_blobs:
                    score = sum(
                        10 for ob in old_blobs for cb in cand_blobs if ob == cb or ob in cb or cb in ob
                    )
                    if old_tokens:
                        cand_tokens = set(" ".join(cand_blobs).split())
                        score += len(old_tokens & cand_tokens)
                    if score > best_score:
                        best_score = score
                        best_cand = cand
            id_mapping.setdefault(old_item.id, best_cand.id)
            for old_bullet in old_item.bullets:
                ob_norm = normalize_match_key(old_bullet.text)
                matched_bullet_id = best_cand.bullets[0].id if best_cand.bullets else best_cand.id
                if ob_norm and best_cand.bullets:
                    for nb in best_cand.bullets:
                        nb_norm = normalize_match_key(nb.text)
                        if nb_norm and (ob_norm == nb_norm or ob_norm in nb_norm or nb_norm in ob_norm):
                            matched_bullet_id = nb.id
                            break
                id_mapping.setdefault(old_bullet.id, matched_bullet_id)

    # 1. Experience partition
    kept_exp: list[RoutedExperienceEntry] = []
    replaced_exp_items: list[RoutedExperienceEntry] = []
    for exp in updated.experience:
        is_fragment = not (exp.company or "").strip() and not (exp.role or "").strip() and not (exp.period or "").strip()
        texts_to_check = [
            t
            for t in [exp.company or "", exp.role or "", exp.details or "", *(b.text for b in exp.bullets)]
            if t.strip()
        ]
        if is_fragment or _is_parser_owned_item(exp.source, exp.evidence, texts_to_check):
            replaced_exp_items.append(exp)
        else:
            kept_exp.append(exp)
            preserved_ids.append(exp.id)

    preserved_exp_sigs = {
        normalize_match_key(f"{e.company or ''}|{e.role or ''}|{e.period or ''}")
        for e in kept_exp
    }
    used_ids: set[str] = set()
    for item_id in [
        *(e.id for e in kept_exp),
        *(b.id for e in kept_exp for b in e.bullets),
        *(v.id for e in kept_exp for b in e.bullets for v in b.variants),
        *(e.id for e in updated.education),
        *(i.id for i in updated.imports),
        *(s.id for s in updated.standard_answers),
        *(sm.id for sm in updated.summaries),
    ]:
        used_ids.add(item_id)

    new_exp_to_add: list[RoutedExperienceEntry] = []
    for cand_exp in reparsed.experience:
        cand_sig = normalize_match_key(f"{cand_exp.company or ''}|{cand_exp.role or ''}|{cand_exp.period or ''}")
        if cand_sig and cand_sig in preserved_exp_sigs:
            continue
        if cand_exp.id in used_ids:
            continue
        new_exp_to_add.append(cand_exp)
        used_ids.add(cand_exp.id)
        for b in cand_exp.bullets:
            used_ids.add(b.id)

    replaced_exp_ids = [e.id for e in replaced_exp_items]
    _map_replaced_entries(replaced_exp_items, new_exp_to_add)

    if replaced_exp_ids:
        grouped_replacements.append(
            {
                "section": "experience",
                "replaced_ids": replaced_exp_ids,
                "replacement_ids": [e.id for e in new_exp_to_add],
                "replacement_count": len(new_exp_to_add),
            }
        )
    updated.experience = (kept_exp + new_exp_to_add)[:MAX_EXPERIENCE_ITEMS]

    # 2. Projects partition
    kept_proj: list[RoutedProjectEntry] = []
    replaced_proj_items: list[RoutedProjectEntry] = []
    for proj in updated.projects:
        is_empty_proj = proj.name in {"Projeto Importado", ""} and not proj.details.strip() and not proj.bullets
        texts_to_check = [t for t in [proj.name, proj.details, *(b.text for b in proj.bullets)] if t.strip()]
        if is_empty_proj or _is_parser_owned_item(proj.source, proj.evidence, texts_to_check):
            replaced_proj_items.append(proj)
        else:
            kept_proj.append(proj)
            preserved_ids.append(proj.id)
            used_ids.add(proj.id)
            for b in proj.bullets:
                used_ids.add(b.id)

    preserved_proj_sigs = {normalize_match_key(p.name) for p in kept_proj}
    new_proj_to_add: list[RoutedProjectEntry] = []
    for cand_proj in reparsed.projects:
        if normalize_match_key(cand_proj.name) in preserved_proj_sigs:
            continue
        if cand_proj.id in used_ids:
            continue
        new_proj_to_add.append(cand_proj)
        used_ids.add(cand_proj.id)
        for b in cand_proj.bullets:
            used_ids.add(b.id)

    replaced_proj_ids = [p.id for p in replaced_proj_items]
    _map_replaced_entries(replaced_proj_items, new_proj_to_add)

    if replaced_proj_ids:
        grouped_replacements.append(
            {
                "section": "projects",
                "replaced_ids": replaced_proj_ids,
                "replacement_ids": [p.id for p in new_proj_to_add],
                "replacement_count": len(new_proj_to_add),
            }
        )
    updated.projects = (kept_proj + new_proj_to_add)[:MAX_PROJECT_ITEMS]

    # 3. Education recovery (preserve all existing valid, add missing recovered)
    kept_edu: list[EducationEntry] = []
    for edu in updated.education:
        if not (edu.institution or "").strip() and not (edu.degree or "").strip() and _is_parser_owned_item(edu.source, edu.evidence, [edu.details]):
            continue
        kept_edu.append(edu)
        if edu.source != f"import:{filename}":
            preserved_ids.append(edu.id)
    existing_edu_sigs = {
        normalize_match_key(f"{e.institution or ''}|{e.degree or ''}|{e.period or ''}")
        for e in kept_edu
    }
    for cand_edu in reparsed.education:
        cand_sig = normalize_match_key(f"{cand_edu.institution or ''}|{cand_edu.degree or ''}|{cand_edu.period or ''}")
        if cand_sig not in existing_edu_sigs and len(kept_edu) < MAX_EDUCATION_ITEMS:
            kept_edu.append(cand_edu)
            existing_edu_sigs.add(cand_sig)
            recovered_education.append(
                {
                    "id": cand_edu.id,
                    "institution": cand_edu.institution,
                    "degree": cand_edu.degree,
                    "period": cand_edu.period,
                }
            )
    updated.education = kept_edu

    # 4. Skills cleanup (drop whitespace/punctuation-only skills + dedup normalized, preserve manual valid)
    cleaned_skills: list[SkillEntry] = []
    seen_sk_keys: set[str] = set()
    for sk in updated.skills:
        nm = sk.name.strip()
        if not nm or not any(ch.isalnum() for ch in nm):
            removed_empty_skills.append(sk.id)
            continue
        key = normalize_match_key(nm)
        if key in seen_sk_keys:
            continue
        cleaned_skills.append(sk)
        seen_sk_keys.add(key)
        if sk.source != f"import:{filename}":
            preserved_ids.append(sk.id)
    for cand_sk in reparsed.skills:
        key = normalize_match_key(cand_sk.name)
        if key not in seen_sk_keys and len(cleaned_skills) < MAX_SKILL_ITEMS:
            cleaned_skills.append(cand_sk)
            seen_sk_keys.add(key)
    updated.skills = cleaned_skills

    # 5. Languages cleanup
    kept_langs: list[LanguageEntry] = []
    seen_lang_keys: set[str] = set()
    for lang in updated.languages:
        key = normalize_match_key(lang.name)
        if not key or key in seen_lang_keys:
            continue
        kept_langs.append(lang)
        seen_lang_keys.add(key)
    for cand_lang in reparsed.languages:
        key = normalize_match_key(cand_lang.name)
        if key not in seen_lang_keys and len(kept_langs) < MAX_LANGUAGE_ITEMS:
            kept_langs.append(cand_lang)
            seen_lang_keys.add(key)
    updated.languages = kept_langs

    # 6. Remap any source_claim_ids on summaries/variants/standard_answers and prune removed orphan targets
    dropped_ids = {
        *[e.id for e in replaced_exp_items],
        *[b.id for e in replaced_exp_items for b in e.bullets],
        *[p.id for p in replaced_proj_items],
        *[b.id for p in replaced_proj_items for b in p.bullets],
    }
    if id_mapping or dropped_ids:
        for summary in updated.summaries:
            remapped = [id_mapping.get(cid, cid) for cid in summary.source_claim_ids]
            summary.source_claim_ids = [cid for cid in remapped if cid not in dropped_ids or cid in id_mapping.values()]
        for ans in updated.standard_answers:
            remapped = [id_mapping.get(cid, cid) for cid in ans.source_claim_ids]
            ans.source_claim_ids = [cid for cid in remapped if cid not in dropped_ids or cid in id_mapping.values()]

    validated = MasterProfileDocumentV2.model_validate(updated.model_dump(mode="python"))
    diff = ImportRepairDiff(
        grouped_replacements=grouped_replacements,
        orphaned_content=reparsed.orphaned_blocks,
        recovered_education=recovered_education,
        removed_empty_skills=removed_empty_skills,
        preserved_user_edited_ids=preserved_ids,
        id_mapping=id_mapping,
        counts=ImportSectionCounts(
            experience=len(new_exp_to_add),
            education=len(recovered_education),
            projects=len(new_proj_to_add),
            skills=len(reparsed.skills),
            languages=len(reparsed.languages),
            bullets=reparsed.total_bullets,
            excluded_blocks=reparsed.excluded_blocks,
        ),
    )
    return validated, diff
