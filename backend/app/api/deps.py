"""Authentication, CSRF, and Origin guard dependencies for local single-user API."""

from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Request, status

from backend.app.core.config import Settings, get_settings
from backend.app.core.security import (
    SessionPayload,
    is_allowed_origin,
    verify_session_token,
)


def get_runtime_settings(request: Request) -> Settings:
    """Resolve settings from app.state if overridden in tests, else global settings."""
    return getattr(request.app.state, "settings", None) or get_settings()


def _extract_token(request: Request) -> tuple[str | None, str]:
    auth_hdr = request.headers.get("Authorization") or ""
    if auth_hdr.lower().startswith("bearer "):
        return auth_hdr[7:].strip(), "bearer"
    cookie_tok = request.cookies.get("cp_session")
    if cookie_tok:
        return cookie_tok.strip(), "cookie"
    return None, "none"


def require_session(
    request: Request,
    settings: Settings = Depends(get_runtime_settings),
) -> SessionPayload:
    """Enforce authenticated local session on protected routes."""
    token, _ = _extract_token(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sessão local não autenticada. Desbloqueie em /api/auth/unlock.",
        )
    payload = verify_session_token(token, settings)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sessão inválida ou expirada.",
        )
    return payload


def require_mutation_auth(
    request: Request,
    settings: Settings = Depends(get_runtime_settings),
    session: SessionPayload = Depends(require_session),
) -> SessionPayload:
    """Enforce session auth, loopback Origin check, and CSRF token for browser cookies."""
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    if not is_allowed_origin(origin, settings):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Origem externa bloqueada pela política CSRF/Origin.",
        )

    _, mode = _extract_token(request)
    csrf_hdr = request.headers.get("X-CSRF-Token") or ""
    if mode == "cookie":
        if not csrf_hdr or not hmac.compare_digest(csrf_hdr, session.csrf_token):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Token CSRF ausente ou inválido.",
            )
    elif csrf_hdr and not hmac.compare_digest(csrf_hdr, session.csrf_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token CSRF inválido.",
        )
    return session
