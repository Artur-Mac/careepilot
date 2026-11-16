"""Pure deterministic candidate validation, slot interpolation, and typed value formatting."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from backend.app.core.ssrf import UnsafeURLError, validate_external_url
from backend.app.core.time import normalize_nfc, utf16_len
from backend.app.schemas.profile import LengthVariants, Locale

ALLOWED_SLOTS: frozenset[str] = frozenset({"company_name", "role_title", "tech_stack_highlight"})
_SLOT_TOKEN_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")
_WORD_TOKEN_RE = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*", re.UNICODE)
_SENTENCE_RE = re.compile(r"[^.!?]+[.!?]+(?:\s+|$)")
_NUMBER_OR_PERCENT_RE = re.compile(r"\d+(?:[.,]\d+)*%?")
_QUALIFIER_RE = re.compile(
    r"\b(nao|não|nem|sem|exceto|apenas|somente|not|no|never|only|without|except|unless)\b",
    re.I,
)
_EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class ControlConstraints:
    """Normalized immutable constraints extracted from a live form control snapshot."""

    control_kind: str
    label: str = ""
    locale: Locale | None = None
    required: bool = False
    minlength: int | None = None
    maxlength: int | None = None
    counter_max_chars: int | None = None
    counter_max_words: int | None = None
    counter_conflict: bool = False
    counter_unsupported: bool = False
    pattern: str | None = None
    minimum: str | None = None
    maximum: str | None = None
    step: str | None = None
    adapter_style: str | None = None

    @property
    def effective_max_chars(self) -> int | None:
        caps = [c for c in (self.maxlength, self.counter_max_chars) if c is not None]
        return min(caps) if caps else None

    @property
    def has_conflict(self) -> bool:
        if self.counter_conflict or self.counter_unsupported:
            return True
        max_c = self.effective_max_chars
        if self.minlength is not None and max_c is not None and self.minlength > max_c:
            return True
        if self.maxlength is not None and self.counter_max_chars is not None and (
            self.maxlength < 0 or self.counter_max_chars < 0
        ):
            return True
        if self.counter_max_words is not None and self.counter_max_words < 0:
            return True
        return bool(self.minlength is not None and self.minlength < 0)


@dataclass(frozen=True, slots=True)
class ValidationOutcome:
    """Result returned by the pure candidate validation pipeline."""

    valid: bool
    value: str | None
    rule_id: str
    intent: str
    source_ids: tuple[str, ...] = ()
    blocker_code: str | None = None
    blocker_message: str | None = None
    trimmed: bool = False
    retention_ratio: float = 1.0


def _parse_nonneg_int(raw: object) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    s = str(raw).strip()
    if s.isdigit():
        return int(s)
    return None


def extract_control_constraints(
    *,
    control_kind: str,
    label: str = "",
    locale: Locale | None = None,
    required: bool = False,
    locator_hint: Mapping[str, object] | None = None,
    adapter_style: str | None = None,
) -> ControlConstraints:
    """Build ControlConstraints safely from DOM locator_hint attributes & visible counters."""
    hint = locator_hint or {}
    raw_limits = hint.get("constraints")
    limits: list[object] = list(raw_limits) if isinstance(raw_limits, (list, tuple)) else []
    while len(limits) < 6:
        limits.append(None)

    minimum = str(limits[0]).strip() if limits[0] is not None and str(limits[0]).strip() else None
    maximum = str(limits[1]).strip() if limits[1] is not None and str(limits[1]).strip() else None
    step = str(limits[2]).strip() if limits[2] is not None and str(limits[2]).strip() else None
    minlength = _parse_nonneg_int(limits[3])
    maxlength = _parse_nonneg_int(limits[4])
    pattern = str(limits[5]) if limits[5] is not None and str(limits[5]) != "" else None

    raw_counters = hint.get("counters")
    counter_max_chars: int | None = None
    counter_max_words: int | None = None
    counter_conflict = False
    counter_unsupported = False
    if isinstance(raw_counters, Mapping):
        counter_max_chars = _parse_nonneg_int(raw_counters.get("max_chars"))
        counter_max_words = _parse_nonneg_int(raw_counters.get("max_words"))
        counter_conflict = bool(raw_counters.get("conflict", False))
        counter_unsupported = bool(raw_counters.get("unsupported", False))

    return ControlConstraints(
        control_kind=control_kind,
        label=label,
        locale=locale,
        required=required,
        minlength=minlength,
        maxlength=maxlength,
        counter_max_chars=counter_max_chars,
        counter_max_words=counter_max_words,
        counter_conflict=counter_conflict,
        counter_unsupported=counter_unsupported,
        pattern=pattern,
        minimum=minimum,
        maximum=maximum,
        step=step,
        adapter_style=adapter_style,
    )


def count_words(text: str, _locale: Locale = "pt-BR") -> int:
    """Count words consistently for pt-BR and English, keeping hyphenated & apostrophe words intact."""
    normalized = normalize_nfc(text)
    return len(_WORD_TOKEN_RE.findall(normalized))


def _is_pattern_safe(pattern: str) -> bool:
    if len(pattern) > 300:
        return False
    # Reject obvious nested quantifiers / catastrophic backtracking patterns
    if re.search(r"\([^)]*[+*][^)]*\)[+*{]", pattern):
        return False
    return not re.search(r"([+*])\1", pattern)


def validate_bounded_pattern(value: str, pattern: str | None) -> tuple[bool, str | None]:
    """Validate whole string against anchored HTML5 pattern with ReDoS safety bounds."""
    if not pattern:
        return True, None
    if not _is_pattern_safe(pattern):
        return False, "unsupported_pattern"
    try:
        compiled = re.compile(rf"\A(?:{pattern})\Z")
    except re.error:
        return False, "unsupported_pattern"
    if compiled.fullmatch(value) is None:
        return False, "pattern_mismatch"
    return True, None


def validate_text_limits(value: str, constraints: ControlConstraints) -> tuple[bool, str | None, str | None]:
    """Check explicit UTF-16 length, word count, and HTML5 pattern limits independently of checkValidity()."""
    if constraints.counter_unsupported:
        return False, "unsupported_counter", "Contador de caracteres/palavras com semântica não suportada no campo."
    if constraints.has_conflict:
        return False, "conflicting_constraints", "Restrições contraditórias detectadas no campo do formulário."

    if _CONTROL_CHAR_RE.search(value):
        return False, "invalid_field_value", "Valor contém caracteres de controle não permitidos."
    if constraints.control_kind in {"text", "email", "tel", "url", "number", "date"} and (
        "\n" in value or "\r" in value
    ):
        return (
            False,
            "length_mismatch",
            "Campo de linha única não aceita múltiplas linhas.",
        )

    char_len = utf16_len(value)
    max_chars = constraints.effective_max_chars
    if constraints.minlength is not None and char_len < constraints.minlength:
        return (
            False,
            "length_mismatch",
            f"Texto abaixo do mínimo de {constraints.minlength} caracteres UTF-16 no campo.",
        )
    if max_chars is not None and char_len > max_chars:
        return (
            False,
            "length_mismatch",
            f"Texto excede o limite máximo de {max_chars} caracteres UTF-16 no campo.",
        )
    if constraints.counter_max_words is not None:
        words = count_words(value, constraints.locale or "pt-BR")
        if words > constraints.counter_max_words:
            return (
                False,
                "length_mismatch",
                f"Texto excede o limite máximo de {constraints.counter_max_words} palavras no campo.",
            )

    pat_ok, pat_code = validate_bounded_pattern(value, constraints.pattern)
    if not pat_ok:
        return False, pat_code or "pattern_mismatch", "Valor não atende ao padrão exigido pelo campo."
    return True, None, None


def non_slot_literal_text(template: str) -> str:
    """Return literal template text excluding any {{slot}} placeholders for retention measurement."""
    return _SLOT_TOKEN_RE.sub("", normalize_nfc(template))


def interpolate_template(
    template: str,
    slot_values: Mapping[str, str],
    *,
    entry_locale: Locale | None = None,
    expected_locale: Locale | None = None,
) -> tuple[bool, str | None, str | None]:
    """Deterministically tokenize and substitute allowed {{slots}} without altering literals."""
    if entry_locale is not None and expected_locale is not None and entry_locale != expected_locale:
        return False, None, "locale_mismatch"

    clean_tpl = normalize_nfc(template)
    # Check for unbalanced {{ or }} outside valid slot tokens
    stripped = _SLOT_TOKEN_RE.sub("", clean_tpl)
    if "{{" in stripped or "}}" in stripped or "{" in stripped or "}" in stripped:
        return False, None, "malformed_slot"

    for match in _SLOT_TOKEN_RE.finditer(clean_tpl):
        slot_name = match.group(1)
        if slot_name not in ALLOWED_SLOTS:
            return False, None, "unknown_slot"
        resolved = (slot_values.get(slot_name) or "").strip()
        if not resolved or "{{" in resolved or "}}" in resolved or _CONTROL_CHAR_RE.search(resolved):
            return False, None, "missing_slot_value"

    def _replace(m: re.Match[str]) -> str:
        val = " ".join(normalize_nfc(slot_values[m.group(1)]).split())
        return val

    rendered = _SLOT_TOKEN_RE.sub(_replace, clean_tpl)
    # Normalize whitespace without altering literal bytes inside retained segments
    rendered = re.sub(r"[ \t]{2,}", " ", rendered).strip()
    if not rendered:
        return False, None, "empty_after_interpolation"
    return True, rendered, None


def _split_complete_sentences(text: str) -> list[str]:
    matches = [m.group(0).strip() for m in _SENTENCE_RE.finditer(text)]
    if not matches:
        return [text] if text.strip() else []
    consumed = sum(len(m.group(0)) for m in _SENTENCE_RE.finditer(text))
    tail = text[consumed:].strip()
    if tail:
        matches.append(tail)
    return matches


def _template_with_sentence_count(template: str, keep_count: int) -> str | None:
    """Slice a template at complete sentence boundaries preserving verbatim literal substrings."""
    sentences = _split_complete_sentences(normalize_nfc(template))
    if keep_count <= 0 or keep_count >= len(sentences):
        return None
    return " ".join(sentences[:keep_count]).strip()


def fit_arsenal_variants(
    variants: LengthVariants,
    slot_values: Mapping[str, str],
    constraints: ControlConstraints,
    *,
    entry_locale: Locale | None = None,
    expected_locale: Locale | None = None,
) -> tuple[str | None, str | None, bool, float]:
    """Select the largest fitting variant (long -> medium -> short) with strict >=95% sentence trim."""
    if constraints.has_conflict:
        code = "unsupported_counter" if constraints.counter_unsupported else "conflicting_constraints"
        return None, code, False, 0.0

    last_err: str | None = "length_mismatch"
    raw_variants = [v for v in (variants.long, variants.medium, variants.short) if v]
    if not raw_variants:
        return None, "missing_answer", False, 0.0

    interpolated_pairs: list[tuple[str, str]] = []
    for raw_tpl in raw_variants:
        ok, rendered, err = interpolate_template(
            raw_tpl,
            slot_values,
            entry_locale=entry_locale,
            expected_locale=expected_locale,
        )
        if not ok or rendered is None:
            last_err = err or "slot_error"
            continue
        interpolated_pairs.append((raw_tpl, rendered))
        fit_ok, limit_code, _ = validate_text_limits(rendered, constraints)
        if fit_ok:
            return rendered, None, False, 1.0
        last_err = limit_code or "length_mismatch"

    # Try complete-sentence trimming only when rendered failed length limits & >= 95% literal retention holds.
    for raw_tpl, _rendered in interpolated_pairs:
        orig_non_slot = non_slot_literal_text(raw_tpl)
        orig_non_slot_u16 = max(1, utf16_len(orig_non_slot))
        sentences = _split_complete_sentences(normalize_nfc(raw_tpl))
        if len(sentences) <= 1:
            continue
        for keep in range(len(sentences) - 1, 0, -1):
            candidate_tpl = _template_with_sentence_count(raw_tpl, keep)
            if not candidate_tpl:
                continue
            dropped_part = " ".join(sentences[keep:]).strip()
            # Never omit material metrics or scope/negation qualifiers
            if _NUMBER_OR_PERCENT_RE.search(dropped_part) or _QUALIFIER_RE.search(dropped_part):
                continue
            kept_non_slot_u16 = utf16_len(non_slot_literal_text(candidate_tpl))
            retention = kept_non_slot_u16 / orig_non_slot_u16
            if retention < 0.95:
                continue
            ok, candidate_rendered, _ = interpolate_template(
                candidate_tpl,
                slot_values,
                entry_locale=entry_locale,
                expected_locale=expected_locale,
            )
            if not ok or candidate_rendered is None:
                continue
            fit_ok, _, _ = validate_text_limits(candidate_rendered, constraints)
            if fit_ok:
                return candidate_rendered, None, True, round(retention, 4)

    return None, last_err, False, 0.0


def validate_and_format_cpf(raw: str, *, style: Literal["digits", "formatted"] = "digits") -> str:
    """Validate 11-digit Brazilian CPF with modulo-11 check digits and format deterministically."""
    digits = "".join(ch for ch in raw.strip() if ch.isdigit())
    if len(digits) != 11 or digits == digits[0] * 11:
        raise ValueError("CPF inválido.")
    nums = [int(c) for c in digits]
    d1 = ((sum(nums[i] * (10 - i) for i in range(9)) * 10) % 11) % 10
    d2 = ((sum(nums[i] * (11 - i) for i in range(10)) * 10) % 11) % 10
    if nums[9] != d1 or nums[10] != d2:
        raise ValueError("Dígitos verificadores do CPF inválidos.")
    if style == "formatted":
        return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"
    return digits


def validate_and_format_cep(raw: str, *, style: Literal["digits", "formatted"] = "formatted") -> str:
    """Validate 8-digit Brazilian CEP retaining leading zeros."""
    digits = "".join(ch for ch in raw.strip() if ch.isdigit())
    if len(digits) != 8 or digits == "00000000":
        raise ValueError("CEP inválido; informe exatamente 8 dígitos.")
    if style == "digits":
        return digits
    return f"{digits[:5]}-{digits[5:]}"


def validate_and_format_phone(
    raw: str,
    *,
    default_country_code: str = "55",
    style: Literal["e164", "br_intl", "br_local", "raw_digits"] = "br_intl",
) -> str:
    """Normalize phone number preserving leading digits and requiring an explicit area code."""
    cleaned = raw.strip()
    if not cleaned:
        raise ValueError("Telefone vazio.")
    has_plus = cleaned.startswith("+")
    digits = "".join(ch for ch in cleaned if ch.isdigit())
    if len(digits) < 10 or len(digits) > 15:
        raise ValueError("Telefone inválido ou sem DDD completo.")

    is_brazil = (default_country_code == "55" and not has_plus and len(digits) in {10, 11}) or (
        digits.startswith("55") and len(digits) in {12, 13}
    )
    if is_brazil:
        national = digits[2:] if (digits.startswith("55") and len(digits) in {12, 13}) else digits
        cc = "55"
        if national[0] == "0":
            # Retain national trunk digits without mutating blindly
            raise ValueError("Remova o zero de operadora e informe DDD + número.")
        ddd, subscriber = national[:2], national[2:]
        if style == "e164":
            return f"+{cc}{national}"
        if style == "raw_digits":
            return national
        prefix = f"+{cc} ({ddd}) " if style == "br_intl" else f"({ddd}) "
        if len(subscriber) == 9:
            return f"{prefix}{subscriber[:5]}-{subscriber[5:]}"
        return f"{prefix}{subscriber[:4]}-{subscriber[4:]}"

    if default_country_code == "55" and not has_plus:
        if digits.startswith("0"):
            raise ValueError("Remova o zero de operadora e informe DDD + número.")
        if len(digits) not in {10, 11} and not (digits.startswith("55") and len(digits) in {12, 13}):
            raise ValueError("Telefone brasileiro requer DDD válido (10 ou 11 dígitos).")

    e164 = f"+{digits}" if has_plus or digits.startswith(default_country_code) else f"+{default_country_code}{digits}"
    if len(e164) < 11 or len(e164) > 16:
        raise ValueError("Telefone internacional fora da faixa E.164.")
    return digits if style == "raw_digits" else e164


def _parse_gregorian_date(raw: str, source_locale: Locale) -> date:
    s = raw.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return date.fromisoformat(s)
    m = re.fullmatch(r"(\d{2})/(\d{2})/(\d{4})", s)
    if m:
        p1, p2, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if source_locale == "pt-BR":
            return date(yr, p2, p1)
        return date(yr, p1, p2)
    raise ValueError("Data inválida; use YYYY-MM-DD ou DD/MM/AAAA conforme o idioma de origem.")


def validate_and_format_date(
    raw: str,
    *,
    control_kind: str,
    source_locale: Locale = "pt-BR",
    target_style: Literal["iso", "pt_br"] | None = None,
    minimum: str | None = None,
    maximum: str | None = None,
) -> str:
    """Validate Gregorian date strictly against known source locale and control min/max bounds."""
    dt = _parse_gregorian_date(raw, source_locale)
    if minimum and re.fullmatch(r"\d{4}-\d{2}-\d{2}", minimum) and dt < date.fromisoformat(minimum):
        raise ValueError(f"Data anterior ao mínimo permitido ({minimum}).")
    if maximum and re.fullmatch(r"\d{4}-\d{2}-\d{2}", maximum) and dt > date.fromisoformat(maximum):
        raise ValueError(f"Data posterior ao máximo permitido ({maximum}).")
    if control_kind == "date" or target_style == "iso":
        return dt.isoformat()
    if target_style == "pt_br" or source_locale == "pt-BR":
        return dt.strftime("%d/%m/%Y")
    return dt.isoformat()


def validate_https_url(raw: str, *, allow_test_origins: set[str] | None = None) -> str:
    """Ensure URL is HTTPS, has no embedded userinfo, and passes SSRF origin checks."""
    from urllib.parse import urlsplit

    cleaned = raw.strip()
    parsed = urlsplit(cleaned)
    if parsed.username is not None or parsed.password is not None or "@" in (parsed.netloc or ""):
        raise ValueError("A URL não pode conter credenciais de usuário ou senha.")
    try:
        validated = validate_external_url(
            cleaned,
            allow_http_public=False,
            allow_test_origins=allow_test_origins,
        )
    except UnsafeURLError as exc:
        raise ValueError(str(exc)) from exc
    if not allow_test_origins and not validated.lower().startswith("https://"):
        raise ValueError("A URL deve usar HTTPS.")
    return validated


def validate_email(raw: str) -> str:
    cleaned = normalize_nfc(raw).strip()
    if not cleaned or len(cleaned) > 254 or any(c.isspace() for c in cleaned):
        raise ValueError("E-mail inválido.")
    if _EMAIL_RE.fullmatch(cleaned) is None:
        raise ValueError("Formato de e-mail inválido.")
    return cleaned


def validate_scalar_number(
    raw: str,
    *,
    minimum: str | None = None,
    maximum: str | None = None,
    step: str | None = None,
) -> str:
    """Validate scalar numeric input cleanly without grouping separators or currency symbols."""
    cleaned = raw.strip()
    if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", cleaned):
        raise ValueError("Número inválido; use ponto decimal sem separadores de milhar ou moeda.")
    try:
        val = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError("Valor numérico inválido.") from exc
    if minimum is not None:
        try:
            min_d = Decimal(minimum)
            if val < min_d:
                raise ValueError(f"Valor abaixo do mínimo permitido ({minimum}).")
        except InvalidOperation:
            pass
    if maximum is not None:
        try:
            max_d = Decimal(maximum)
            if val > max_d:
                raise ValueError(f"Valor acima do máximo permitido ({maximum}).")
        except InvalidOperation:
            pass
    if step is not None and step.lower() != "any":
        try:
            step_d = Decimal(step)
            if step_d > 0:
                base = Decimal(minimum) if minimum is not None else Decimal("0")
                if (val - base) % step_d != 0:
                    raise ValueError(f"Valor incompatível com o incremento step={step}.")
        except InvalidOperation:
            pass
    return cleaned


def _infer_cpf_style(constraints: ControlConstraints) -> Literal["digits", "formatted"]:
    if constraints.adapter_style in {"digits", "formatted"}:
        return constraints.adapter_style  # type: ignore[return-value]
    if constraints.pattern and (r"\." in constraints.pattern or "-" in constraints.pattern):
        return "formatted"
    if constraints.effective_max_chars == 11 or (
        constraints.pattern and ("{11}" in constraints.pattern or constraints.pattern == r"\d+")
    ):
        return "digits"
    if constraints.effective_max_chars is not None and constraints.effective_max_chars < 14:
        return "digits"
    return "formatted" if constraints.pattern and "." in constraints.pattern else "digits"


def _infer_cep_style(constraints: ControlConstraints) -> Literal["digits", "formatted"]:
    if constraints.adapter_style in {"digits", "formatted"}:
        return constraints.adapter_style  # type: ignore[return-value]
    if constraints.pattern and "-" in constraints.pattern:
        return "formatted"
    if constraints.effective_max_chars == 8:
        return "digits"
    return "formatted" if constraints.effective_max_chars == 9 else "digits"


def _infer_phone_style(
    constraints: ControlConstraints,
) -> Literal["e164", "br_intl", "br_local", "raw_digits"]:
    if constraints.adapter_style in {"e164", "br_intl", "br_local", "raw_digits"}:
        return constraints.adapter_style  # type: ignore[return-value]
    max_c = constraints.effective_max_chars
    if constraints.pattern and r"\+" in constraints.pattern:
        return "e164" if (max_c is not None and max_c < 16) else "br_intl"
    if max_c is not None:
        if max_c <= 11:
            return "raw_digits"
        if max_c <= 15:
            return "br_local"
    return "br_intl"


def validate_candidate_value(
    constraints: ControlConstraints,
    raw_value: str,
    *,
    rule_id: str,
    intent: str,
    source_ids: tuple[str, ...] = (),
    source_locale: Locale = "pt-BR",
    allow_test_origins: set[str] | None = None,
) -> ValidationOutcome:
    """Pure validator applying typed formatting and HTML constraint checks before DOM writes."""
    if constraints.counter_unsupported:
        return ValidationOutcome(
            False,
            None,
            rule_id,
            intent,
            source_ids,
            "unsupported_counter",
            "Contador visível com unidade ou formato não suportado.",
        )
    if constraints.has_conflict:
        return ValidationOutcome(
            False,
            None,
            rule_id,
            intent,
            source_ids,
            "conflicting_constraints",
            "Restrições mínimas e máximas do campo entram em conflito.",
        )

    formatted = normalize_nfc(raw_value).strip()
    try:
        if intent == "cpf":
            formatted = validate_and_format_cpf(formatted, style=_infer_cpf_style(constraints))
        elif intent == "cep":
            formatted = validate_and_format_cep(formatted, style=_infer_cep_style(constraints))
        elif intent == "phone" or constraints.control_kind == "tel":
            style = _infer_phone_style(constraints)
            try:
                formatted = validate_and_format_phone(formatted, style=style)
            except ValueError:
                # Re-try digits fallback if control has tight cap
                if style != "raw_digits":
                    formatted = validate_and_format_phone(formatted, style="raw_digits")
                else:
                    raise
        elif intent == "email" or constraints.control_kind == "email":
            formatted = validate_email(formatted)
        elif intent == "url" or constraints.control_kind == "url":
            formatted = validate_https_url(formatted, allow_test_origins=allow_test_origins)
        elif intent == "date" or constraints.control_kind == "date":
            date_style: Literal["iso", "pt_br"] | None = (
                "iso" if constraints.control_kind == "date" else "pt_br"
            )
            if constraints.adapter_style in {"iso", "pt_br"}:
                date_style = constraints.adapter_style  # type: ignore[assignment]
            formatted = validate_and_format_date(
                formatted,
                control_kind=constraints.control_kind,
                source_locale=source_locale,
                target_style=date_style,
                minimum=constraints.minimum,
                maximum=constraints.maximum,
            )
        elif constraints.control_kind == "number":
            formatted = validate_scalar_number(
                formatted,
                minimum=constraints.minimum,
                maximum=constraints.maximum,
                step=constraints.step,
            )
    except ValueError as exc:
        code_map = {
            "cpf": "invalid_cpf",
            "cep": "invalid_cep",
            "phone": "invalid_phone",
            "email": "invalid_email",
            "url": "invalid_url",
            "date": "invalid_date",
        }
        err_code = code_map.get(intent, "invalid_field_value")
        return ValidationOutcome(False, None, rule_id, intent, source_ids, err_code, str(exc))

    ok, blk_code, blk_msg = validate_text_limits(formatted, constraints)
    if not ok:
        return ValidationOutcome(False, None, rule_id, intent, source_ids, blk_code, blk_msg)

    return ValidationOutcome(True, formatted, rule_id, intent, source_ids)


def normalize_match_tokens(text: str) -> str:
    """Accent-folded lowercase token normalization used in deterministic matching."""
    folded = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", stripped.lower()).strip()


def validate_iso_timestamp(value: str) -> datetime:
    """Validate timezone-aware RFC 3339 timestamp helper."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Timestamp sem fuso horário.")
    return dt
