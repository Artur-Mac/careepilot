"""Optional Gmail API desktop OAuth connector + job-alert & opt-in reply ingestion."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.app.core.config import Settings
from backend.app.schemas.contracts import NormalizedJob
from backend.app.security.prompt_guard import sanitize_untrusted_text

# Least-privilege split: reading alerts does not grant send scope; sending requires separate approval.
GMAIL_READ_SCOPES: tuple[str, ...] = ("https://www.googleapis.com/auth/gmail.readonly",)
GMAIL_SEND_SCOPES: tuple[str, ...] = ("https://www.googleapis.com/auth/gmail.send",)


@dataclass(frozen=True, slots=True)
class GmailStatus:
    configured: bool
    has_token: bool
    mode: str
    read_scope: str
    send_scope: str
    setup_steps: list[str]


class GmailConnector:
    """Optional Gmail OAuth desktop connector with safe local fixture ingestion."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def status(self) -> GmailStatus:
        secrets_ok = bool(
            self.settings.gmail_client_secrets_path
            and Path(self.settings.gmail_client_secrets_path).exists()
        )
        token_ok = bool(
            self.settings.gmail_token_path and Path(self.settings.gmail_token_path).exists()
        )
        return GmailStatus(
            configured=secrets_ok,
            has_token=token_ok,
            mode="oauth_connected" if (secrets_ok and token_ok) else "local_fixture_only",
            read_scope=GMAIL_READ_SCOPES[0],
            send_scope=GMAIL_SEND_SCOPES[0],
            setup_steps=[
                "1. Crie credenciais OAuth Desktop no Google Cloud Console (sem publicar em produção).",
                "2. Salve client_secret.json fora do repositório e aponte GMAIL_CLIENT_SECRETS_PATH no .env.",
                "3. Autorize inicialmente apenas o escopo gmail.readonly para ler alertas de vagas.",
                "4. E-mails nunca recebem CPF, RG, endereço residencial ou segredos OAuth.",
            ],
        )

    def parse_job_alert_email(self, *, subject: str, sender: str, body: str) -> list[NormalizedJob]:
        """Extract job URLs and metadata from official LinkedIn/Indeed/Gupy alert emails (always manual_only)."""
        clean = sanitize_untrusted_text(body).text
        urls = re.findall(r"https://[^\s<>\"']+", clean)
        now = datetime.now(UTC)
        source_name = "email_alert"
        low_sender = f"{sender} {subject}".lower()
        if "linkedin" in low_sender:
            source_name = "linkedin_email_alert"
        elif "indeed" in low_sender:
            source_name = "indeed_email_alert"
        elif "gupy" in low_sender:
            source_name = "gupy_email_alert"

        jobs: list[NormalizedJob] = []
        for idx, url in enumerate(urls[:10]):
            try:
                jobs.append(
                    NormalizedJob(
                        source=source_name,
                        source_job_id=f"mail-{idx}-{abs(hash(url)) % 1000000}",
                        company="Via Alerta de E-mail",
                        title=subject[:180] or "Vaga recebida por alerta",
                        location=None,
                        workplace_type="unknown",
                        description=clean[:2000] or subject,
                        job_url=url,
                        apply_url=url,
                        posted_at=now,
                        discovered_at=now,
                        source_attribution=f"Alerta oficial por e-mail ({source_name})",
                        discovery_channel="email",
                    )
                )
            except Exception:
                continue
        return jobs

    def detect_recruiter_reply(self, *, subject: str, sender: str, body: str) -> dict[str, Any]:
        """Opt-in recruiter reply classifier — read-only, NEVER sends automated responses."""
        clean = sanitize_untrusted_text(body).text
        low = f"{subject} {clean}".lower()
        category = "informational"
        if any(k in low for k in ("entrevista", "interview", "agenda", "conversa", "horário")):
            category = "interview_invite"
        elif any(k in low for k in ("infelizmente", "não seguiremos", "unfortunately", "encerrada")):
            category = "rejection"
        elif any(k in low for k in ("recebemos", "candidatura confirmada", "application received")):
            category = "acknowledgement"
        return {
            "sender": sender,
            "subject": subject,
            "category": category,
            "snippet": clean[:300],
            "auto_reply_allowed": False,
        }
