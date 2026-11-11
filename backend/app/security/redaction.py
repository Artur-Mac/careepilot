"""Secret and sensitive PII redaction utilities for logs, snapshots, and errors."""

from __future__ import annotations

import re
from collections.abc import Iterable

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/]+=*"), r"\1[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), "[REDACTED_API_KEY]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"), "[REDACTED_GOOGLE_KEY]"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs)_[A-Za-z0-9]{20,}\b"), "[REDACTED_GH_TOKEN]"),
    (re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{24,}\b"), "[REDACTED_TELEGRAM_TOKEN]"),
    (
        re.compile(
            r'(?i)("?(?:access_token|refresh_token|client_secret|api_key|authorization|password|passcode)"?\s*[:=]\s*"?)([^"\s,}]+)'
        ),
        r"\1[REDACTED]",
    ),
    # Brazilian CPF formats (000.000.000-00 or explicitly labeled CPF)
    (re.compile(r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b"), "[REDACTED_CPF]"),
    (re.compile(r"(?i)(cpf\s*[:=]?\s*)\d{11}\b"), r"\1[REDACTED_CPF]"),
    # Brazilian RG when explicitly labeled
    (re.compile(r"(?i)(rg\s*[:=]?\s*)[0-9A-Za-z.\-]{6,14}"), r"\1[REDACTED_RG]"),
)


def redact_secrets(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Mask API tokens, OAuth credentials, passcodes, and sensitive Brazilian IDs."""
    if not text:
        return text
    cleaned = text
    for secret in extra_secrets:
        if secret and len(secret) >= 6:
            cleaned = cleaned.replace(secret, "[REDACTED_SECRET]")
    for pattern, replacement in _SECRET_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


def redact_dict(data: object, extra_secrets: Iterable[str] = ()) -> object:
    """Recursively redact sensitive dictionary keys and string values."""
    sensitive_keys = {
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "client_secret",
        "authorization",
        "password",
        "passcode",
        "cpf",
        "rg",
        "full_address",
        "government_id",
        "token",
        "secret",
    }
    if isinstance(data, dict):
        result: dict[str, object] = {}
        for k, v in data.items():
            if str(k).lower() in sensitive_keys:
                result[str(k)] = "[REDACTED]"
            else:
                result[str(k)] = redact_dict(v, extra_secrets)
        return result
    if isinstance(data, list):
        return [redact_dict(item, extra_secrets) for item in data]
    if isinstance(data, str):
        return redact_secrets(data, extra_secrets)
    return data
