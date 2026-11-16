"""Shared UTC RFC 3339 timestamps, NFC Unicode normalization, and UTF-16 length helpers."""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime

_RFC3339_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


def utc_rfc3339(dt: datetime | None = None) -> str:
    """Format a timezone-aware datetime (or current UTC time) at fixed microsecond precision.

    Fixed-width fractional seconds ensure chronological string ordering inside SQLite TEXT checks
    (e.g., `expires_at > checked_at` and cursor pagination).
    """
    target = dt if dt is not None else datetime.now(UTC)
    if target.tzinfo is None:
        raise ValueError("Timestamp must be timezone-aware.")
    return target.astimezone(UTC).strftime(_RFC3339_FMT)


def parse_utc_rfc3339(raw: str | datetime) -> datetime:
    """Parse an ISO/RFC 3339 timestamp, rejecting naive timestamps without timezone offset."""
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            raise ValueError("Timestamp must include a timezone offset.")
        return raw.astimezone(UTC)
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include a timezone offset.")
    return parsed.astimezone(UTC)


def normalize_nfc(text: str) -> str:
    """Normalize Unicode strings to NFC without mutating whitespace semantics."""
    return unicodedata.normalize("NFC", text)


def utf16_len(text: str) -> int:
    """Return length in UTF-16 code units, matching HTML maxlength/selection behavior."""
    return len(text.encode("utf-16-le")) // 2
