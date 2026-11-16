"""Sensitive PII classification and safe local vault policy helpers."""

from __future__ import annotations

import base64
import hashlib
from enum import StrEnum

from cryptography.fernet import Fernet


class SensitivityLevel(StrEnum):
    """Data classification for profile facts and application fields."""

    PUBLIC = "public"
    CONTACT = "contact"
    SENSITIVE_PII = "sensitive_pii"


SENSITIVE_FACT_CATEGORIES: frozenset[str] = frozenset(
    {
        "cpf",
        "rg",
        "government_id",
        "passport",
        "full_address",
        "street_address",
        "birth_date",
        "bank_account",
    }
)


def classify_field_sensitivity(field_name: str) -> SensitivityLevel:
    """Classify field sensitivity so CPF/RG/full address are never leaked to logs, LLMs, or email."""
    import re

    lower = field_name.strip().lower()
    if any(k in lower for k in ("cpf", "government_id", "passport", "address", "endereco", "endereço", "birth")) or re.search(
        r"\brg\b", lower
    ):
        return SensitivityLevel.SENSITIVE_PII
    if any(k in lower for k in ("email", "phone", "telefone", "linkedin", "github", "portfolio")):
        return SensitivityLevel.CONTACT
    return SensitivityLevel.PUBLIC


def is_safe_for_notification(field_name: str) -> bool:
    """Ensure sensitive government IDs and full addresses are never placed in notifications/emails."""
    return classify_field_sensitivity(field_name) != SensitivityLevel.SENSITIVE_PII


def derive_local_fernet(secret_material: str) -> Fernet:
    """Derive a Fernet key from local secret material for optional future encrypted field storage."""
    digest = hashlib.sha256(secret_material.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))
