"""Structured logging with automatic secret redaction."""

from __future__ import annotations

import logging

from backend.app.core.config import get_settings
from backend.app.security.redaction import redact_secrets


class RedactingFilter(logging.Filter):
    """Scrub secrets and Brazilian PII patterns from log records before emission."""

    def filter(self, record: logging.LogRecord) -> bool:
        secrets = get_settings().secret_values()
        if isinstance(record.msg, str):
            record.msg = redact_secrets(record.msg, secrets)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    redact_secrets(str(a), secrets) if isinstance(a, str) else a for a in record.args
                )
            elif isinstance(record.args, dict):
                record.args = {
                    k: redact_secrets(str(v), secrets) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
        return True


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure root CareerPilot logger with redaction filter."""
    logger = logging.getLogger("careerpilot")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
        )
        handler.addFilter(RedactingFilter())
        logger.addHandler(handler)
    logger.setLevel(level)
    return logger
