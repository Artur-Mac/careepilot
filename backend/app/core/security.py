"""Local single-user session authentication, CSRF protection, and origin validation."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

from backend.app.core.config import Settings


@dataclass(frozen=True, slots=True)
class SessionPayload:
    """Verified local session claims."""

    user_id: str
    csrf_token: str
    issued_at: int
    expires_at: int


def _sign(payload_b64: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()


def issue_session_token(settings: Settings, user_id: str = "local-applicant") -> tuple[str, str]:
    """Create a signed local session token and bound CSRF token."""
    now = int(time.time())
    csrf = secrets.token_urlsafe(24)
    payload = {
        "uid": user_id,
        "csrf": csrf,
        "iat": now,
        "exp": now + settings.session_ttl_seconds,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    import base64

    b64 = base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")
    sig = _sign(b64, settings.careerpilot_session_secret)
    return f"{b64}.{sig}", csrf


def verify_session_token(token: str, settings: Settings) -> SessionPayload | None:
    """Verify HMAC signature and expiry of a local session token."""
    if not token or "." not in token:
        return None
    b64, sig = token.split(".", 1)
    expected = _sign(b64, settings.careerpilot_session_secret)
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        import base64

        data = json.loads(base64.urlsafe_b64decode(b64.encode("ascii")).decode("utf-8"))
        now = int(time.time())
        if int(data["exp"]) < now:
            return None
        return SessionPayload(
            user_id=str(data["uid"]),
            csrf_token=str(data["csrf"]),
            issued_at=int(data["iat"]),
            expires_at=int(data["exp"]),
        )
    except Exception:
        return None


def verify_passcode(provided: str, settings: Settings) -> bool:
    """Constant-time verification of the local unlock passcode."""
    return hmac.compare_digest(provided.strip(), settings.careerpilot_passcode.strip())


def is_allowed_origin(origin_or_referer: str | None, settings: Settings) -> bool:
    """Reject cross-origin requests from external websites even when server runs on localhost."""
    if not origin_or_referer:
        return True  # Non-browser clients (curl/httpx tests) omit Origin; still need valid AuthN.
    from backend.app.core.ssrf import extract_origin

    extracted = extract_origin(origin_or_referer)
    # Allow loopback ports used in local UI / test servers and TestClient dynamic origin
    return extracted in settings.allowed_origins or extracted in {
        "http://testserver",
        "https://testserver",
    }
