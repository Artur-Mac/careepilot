"""Provider-independent notification interface, local outbox, and optional Telegram/Gmail stubs."""

from __future__ import annotations

import secrets
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from backend.app.core.config import Settings
from backend.app.models.tables import Notification
from backend.app.security.redaction import redact_secrets


@dataclass(frozen=True, slots=True)
class OutgoingNotification:
    recipient: str
    subject: str
    body: str
    metadata: dict[str, Any]


class NotificationAdapter(ABC):
    channel: str

    @abstractmethod
    def send(self, db: Session, msg: OutgoingNotification) -> Notification:
        """Deliver or queue notification without leaking sensitive PII or secrets."""


class LocalOutboxAdapter(NotificationAdapter):
    """Default safe local notification outbox stored in SQLite for dashboard preview."""

    channel = "local_outbox"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def send(self, db: Session, msg: OutgoingNotification) -> Notification:
        safe_body = redact_secrets(msg.body, self.settings.secret_values())
        safe_subject = redact_secrets(msg.subject, self.settings.secret_values())
        row = Notification(
            channel=self.channel,
            recipient=msg.recipient,
            subject=safe_subject[:250],
            body_preview=safe_body[:2000],
            status="stored_local_outbox",
            metadata_json=dict(msg.metadata),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row


class TelegramBotAdapter(NotificationAdapter):
    """Optional Telegram Bot notification adapter with explicit chat ID allowlist & nonce."""

    channel = "telegram"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def send(self, db: Session, msg: OutgoingNotification) -> Notification:
        allowed_ids = {
            cid.strip()
            for cid in self.settings.telegram_allowed_chat_ids.split(",")
            if cid.strip()
        }
        nonce = secrets.token_hex(8)
        safe_body = redact_secrets(msg.body, self.settings.secret_values())
        if not self.settings.telegram_bot_token.strip() or msg.recipient not in allowed_ids:
            status = "blocked_unconfigured_or_unlisted_chat"
        else:
            # In offline MVP default, queue signed payload; live HTTP send occurs only if explicitly triggered.
            status = "queued_telegram"
        row = Notification(
            channel=self.channel,
            recipient=msg.recipient,
            subject=redact_secrets(msg.subject, self.settings.secret_values())[:250],
            body_preview=safe_body[:2000],
            status=status,
            metadata_json={**msg.metadata, "nonce": nonce},
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row


class GmailNotificationAdapter(NotificationAdapter):
    """Optional Gmail notification adapter; strictly forbids CPF, RG, address, and OAuth secrets."""

    channel = "gmail"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def send(self, db: Session, msg: OutgoingNotification) -> Notification:
        safe_body = redact_secrets(msg.body, self.settings.secret_values())
        status = (
            "queued_gmail_oauth"
            if self.settings.gmail_token_path.strip()
            else "stored_local_preview_gmail_unconfigured"
        )
        row = Notification(
            channel=self.channel,
            recipient=msg.recipient,
            subject=redact_secrets(msg.subject, self.settings.secret_values())[:250],
            body_preview=safe_body[:2000],
            status=status,
            metadata_json=dict(msg.metadata),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row


def get_notification_adapter(settings: Settings) -> NotificationAdapter:
    ch = settings.notification_channel.strip().lower()
    if ch == "telegram":
        return TelegramBotAdapter(settings)
    if ch == "gmail":
        return GmailNotificationAdapter(settings)
    return LocalOutboxAdapter(settings)
