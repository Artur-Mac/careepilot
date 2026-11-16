"""Portal session registry, bounded pre-flight probing, and persisted status store."""

from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.browser_automation.session import BrowserSession, BrowserUnavailableError
from backend.app.core.ssrf import UnsafeURLError, extract_origin
from backend.app.core.time import parse_utc_rfc3339, utc_rfc3339
from backend.app.models.tables import Application, JobPosting, PortalSessionStatusRow

PortalAuthModel = Literal["requires_login", "public", "tenant_dependent"]
PortalStatusLiteral = Literal[
    "unknown",
    "checking",
    "authenticated",
    "login_required",
    "public_access",
    "challenge",
    "unavailable",
    "disconnected",
]

SESSION_TTL_SECONDS = 300
CHECKING_STALE_SECONDS = 30
PROBE_TIMEOUT_SECONDS = 2.5


class PreflightPortalError(RuntimeError):
    """Domain error for portal registry and login handoff validation."""

    def __init__(self, code: str, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class PortalRegistryEntry:
    """App-owned metadata and verification rules for a supported portal."""

    portal_key: str
    name: str
    auth_model: PortalAuthModel
    discovery_capable: bool
    valid_domains: tuple[str, ...]
    login_url: str | None
    default_scope: str
    login_route_patterns: tuple[re.Pattern[str], ...]
    public_posting_patterns: tuple[re.Pattern[str], ...] = ()
    positive_selectors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PortalScope:
    """Concrete portal + tenant/scope verification target."""

    portal_key: str
    scope_key: str
    entry_url: str | None = None


PORTAL_REGISTRY: dict[str, PortalRegistryEntry] = {
    "linkedin": PortalRegistryEntry(
        portal_key="linkedin",
        name="LinkedIn",
        auth_model="requires_login",
        discovery_capable=True,
        valid_domains=("linkedin.com",),
        login_url="https://www.linkedin.com/login",
        default_scope="www.linkedin.com",
        login_route_patterns=(re.compile(r"/(?:uas/)?login|checkpoint|authwall", re.I),),
        public_posting_patterns=(re.compile(r"/jobs/view/\d+"),),
        positive_selectors=(
            "button.global-nav__primary-link-me-menu-trigger",
            "[data-test-global-nav]",
            "a[href*='/mynetwork/']",
        ),
    ),
    "gupy": PortalRegistryEntry(
        portal_key="gupy",
        name="Gupy",
        auth_model="tenant_dependent",
        discovery_capable=True,
        valid_domains=("gupy.io",),
        login_url="https://portal.gupy.io/",
        default_scope="portal.gupy.io",
        login_route_patterns=(re.compile(r"/(?:login|signin|entrar|auth)", re.I),),
        public_posting_patterns=(re.compile(r"/job/\d+|/jobs/\d+"),),
        positive_selectors=(
            "[data-testid*='user-menu']",
            "[data-testid*='candidate']",
            "button[aria-label*='perfil' i]",
        ),
    ),
    "inhire": PortalRegistryEntry(
        portal_key="inhire",
        name="Inhire",
        auth_model="tenant_dependent",
        discovery_capable=False,
        valid_domains=("inhire.jobs", "inhire.com.br"),
        login_url=None,
        default_scope="default",
        login_route_patterns=(re.compile(r"/(?:login|entrar|auth)", re.I),),
        public_posting_patterns=(re.compile(r"/vagas/|/jobs/"),),
        positive_selectors=("[data-testid*='account']",),
    ),
    "greenhouse": PortalRegistryEntry(
        portal_key="greenhouse",
        name="Greenhouse",
        auth_model="tenant_dependent",
        discovery_capable=False,
        valid_domains=("greenhouse.io",),
        login_url=None,
        default_scope="default",
        login_route_patterns=(re.compile(r"/(?:users/sign_in|login|auth)", re.I),),
        public_posting_patterns=(re.compile(r"/jobs/\d+(?:/|$)"),),
        positive_selectors=("a[href*='/sign_out']", "[data-testid*='account']"),
    ),
    "lever": PortalRegistryEntry(
        portal_key="lever",
        name="Lever",
        auth_model="tenant_dependent",
        discovery_capable=False,
        valid_domains=("lever.co",),
        login_url=None,
        default_scope="default",
        login_route_patterns=(re.compile(r"/(?:login|signin|auth)", re.I),),
        public_posting_patterns=(re.compile(r"/[a-zA-Z0-9_-]+/[0-9a-fA-F-]{36}(?:/|$)"),),
        positive_selectors=("[data-qa='account-menu']",),
    ),
    "workday": PortalRegistryEntry(
        portal_key="workday",
        name="Workday",
        auth_model="tenant_dependent",
        discovery_capable=False,
        valid_domains=("myworkdayjobs.com", "myworkdaysite.com"),
        login_url=None,
        default_scope="default",
        login_route_patterns=(re.compile(r"/(?:login|sign-?in|auth)", re.I),),
        public_posting_patterns=(re.compile(r"/job/[^/]+|/details/"),),
        positive_selectors=(
            "[data-automation-id='utilityButtonSignOut']",
            "[data-automation-id='candidateHomeLink']",
            "button[data-automation-id='signedInUser']",
        ),
    ),
    "vagas": PortalRegistryEntry(
        portal_key="vagas",
        name="Vagas.com",
        auth_model="requires_login",
        discovery_capable=False,
        valid_domains=("vagas.com.br",),
        login_url="https://www.vagas.com.br/login-candidatos",
        default_scope="www.vagas.com.br",
        login_route_patterns=(re.compile(r"/login|/candidatos/login", re.I),),
        public_posting_patterns=(re.compile(r"/vagas/v\d+"),),
        positive_selectors=("a[href*='/sair']", ".user-menu", "[data-testid*='usuario']"),
    ),
    "infojobs": PortalRegistryEntry(
        portal_key="infojobs",
        name="InfoJobs",
        auth_model="requires_login",
        discovery_capable=True,
        valid_domains=("infojobs.com.br",),
        login_url="https://www.infojobs.com.br/",
        default_scope="www.infojobs.com.br",
        login_route_patterns=(re.compile(r"/login|/candidate/login", re.I),),
        public_posting_patterns=(re.compile(r"/vaga-de-"),),
        positive_selectors=("a[href*='/sair']",),
    ),
    "google": PortalRegistryEntry(
        portal_key="google",
        name="Google",
        auth_model="public",
        discovery_capable=True,
        valid_domains=("google.com", "google.com.br"),
        login_url="https://www.google.com/",
        default_scope="www.google.com",
        login_route_patterns=(re.compile(r"ServiceLogin|accounts\.google", re.I),),
        public_posting_patterns=(re.compile(r"^/(?:search)?$"),),
        positive_selectors=("a[aria-label*='Google Account' i]",),
    ),
}

# Compatibility alias preserving (name, url) mapping for existing consumers.
PORTALS: dict[str, tuple[str, str]] = {
    key: (entry.name, entry.login_url or f"https://{entry.valid_domains[0]}/")
    for key, entry in PORTAL_REGISTRY.items()
}

# Track in-flight per-scope verification tasks so one probe runs per scope key at a time.
_ACTIVE_PROBE_TASKS: dict[tuple[str, str, str], asyncio.Task[None]] = {}


def _host_matches_domain(host: str, domains: tuple[str, ...]) -> bool:
    clean = host.strip().lower()
    return any(clean == dom or clean.endswith("." + dom) for dom in domains)


def _match_portal_for_url(
    url: str, *, allow_test_origins: set[str] | None = None
) -> tuple[str, str] | None:
    parsed = urlparse((url or "").strip())
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    origin = extract_origin(url) if scheme in {"http", "https"} else ""
    if scheme != "https" and origin.lower() not in (allow_test_origins or set()):
        return None
    for key, entry in PORTAL_REGISTRY.items():
        if _host_matches_domain(host, entry.valid_domains):
            scope = parsed.netloc.lower() if entry.auth_model == "tenant_dependent" else entry.default_scope
            return key, scope
    return None


def _url_matches_scope(
    url: str,
    entry: PortalRegistryEntry,
    scope: PortalScope,
    *,
    allow_test_origins: set[str],
) -> bool:
    parsed = urlparse((url or "").strip())
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    netloc = parsed.netloc.lower()
    if not host:
        return False
    origin = extract_origin(url) if scheme in {"http", "https"} else ""
    allowed_test = origin.lower() in allow_test_origins
    if scheme != "https" and not allowed_test:
        return False

    if scope.entry_url:
        try:
            target_origin = extract_origin(scope.entry_url)
            if target_origin and origin == target_origin:
                return True
        except UnsafeURLError:
            pass

    if allowed_test and scope.scope_key.lower() in {host, netloc, origin.lower()}:
        return True

    if not _host_matches_domain(host, entry.valid_domains):
        return False
    if entry.auth_model == "tenant_dependent" and scope.scope_key != "default":
        return scope.scope_key.lower() in {host, netloc}
    return True


async def _evaluate_page_signals(page: Page, entry: PortalRegistryEntry) -> dict[str, Any]:
    return dict(
        await page.evaluate(
            """(extraPositive) => {
                const visible = (el) => !!el && el.isConnected
                    && getComputedStyle(el).display !== 'none'
                    && getComputedStyle(el).visibility !== 'hidden'
                    && getComputedStyle(el).opacity !== '0'
                    && el.getClientRects().length > 0;
                const has = (sel) => {
                    try {
                        return [...document.querySelectorAll(sel)].some(visible);
                    } catch (_) {
                        return false;
                    }
                };
                const bodyText = (document.body?.innerText || '').slice(0, 6000).toLowerCase();
                const hasPassword = has('input[type="password"]');
                const hasLoginForm = hasPassword && (
                    has('form[action*="login" i], form[action*="signin" i], form[action*="auth" i]')
                    || /\\b(entrar|fazer login|acesse sua conta|sign in|log in)\\b/i.test(bodyText)
                );
                const hasLogout = has('a[href*="/logout"], a[href*="/signout"], a[href*="/sign_out"], a[href*="/sair"], form[action*="/logout"]');
                const hasExtraPositive = (extraPositive || []).some((sel) => has(sel));
                const hasChallenge = has(
                    'iframe[src*="captcha" i], iframe[src*="turnstile" i], .g-recaptcha, .h-captcha, .cf-challenge, input[autocomplete="one-time-code"], [data-testid*="captcha" i]'
                ) || /verify you are human|checking your browser|confirme que voce e humano|recaptcha/i.test(bodyText);
                const hasRateLimit = /http 429|too many requests|muitas requisicoes|rate limit/i.test(bodyText);
                const hasAccessBlock = /http 403|access denied|acesso bloqueado|forbidden|cloudflare ray id/i.test(bodyText);
                const hasHeading = has('main h1, h1');
                const applyRequiresLogin = /entrar para candidatar|faca login para se candidatar|sign in to apply/i.test(bodyText);
                return {
                    hasPassword,
                    hasLoginForm,
                    hasLoginText: /\\b(faca login|entrar na conta|sign in to continue|please sign in)\\b/i.test(bodyText),
                    hasAccount: hasLogout || hasExtraPositive,
                    hasChallenge,
                    hasRateLimit,
                    hasAccessBlock,
                    hasHeading,
                    applyRequiresLogin,
                };
            }""",
            list(entry.positive_selectors),
        )
    )


def _classify_probe_signals(
    *,
    entry: PortalRegistryEntry,
    original_url: str,
    final_url: str,
    signals: dict[str, Any],
) -> tuple[PortalStatusLiteral, str | None]:
    if signals.get("hasChallenge"):
        return "challenge", "challenge_present"
    if signals.get("hasRateLimit"):
        return "unavailable", "rate_limited"
    if signals.get("hasAccessBlock"):
        return "unavailable", "access_blocked"

    orig_path = urlparse(original_url).path
    final_path = urlparse(final_url).path
    orig_is_login = any(p.search(original_url) for p in entry.login_route_patterns)
    final_is_login = any(p.search(final_url) for p in entry.login_route_patterns)
    if final_is_login and not orig_is_login and final_url != original_url:
        return "login_required", "login_redirect"
    if final_is_login or (signals.get("hasPassword") and (signals.get("hasLoginForm") or signals.get("hasLoginText"))):
        return "login_required", "login_form_present"
    if signals.get("hasAccount") and not final_is_login:
        return "authenticated", None

    is_public_posting = any(
        p.search(final_path) or p.search(orig_path) for p in entry.public_posting_patterns
    )
    if (
        entry.auth_model in {"public", "tenant_dependent"}
        and (is_public_posting or entry.auth_model == "public")
        and signals.get("hasHeading")
        and not signals.get("hasPassword")
    ):
        reason = "tenant_apply_login_required" if signals.get("applyRequiresLogin") else None
        return "public_access", reason

    return "unknown", "adapter_unverified"


async def probe_session(
    scope: PortalScope,
    browser: BrowserSession,
    *,
    page: Page | None = None,
) -> dict[str, Any]:
    """Read-only verification probe for a single portal scope against the visible CDP browser."""
    now = datetime.now(UTC)
    now_iso = now.isoformat()
    expires_iso = (now + timedelta(seconds=SESSION_TTL_SECONDS)).isoformat()
    entry = PORTAL_REGISTRY.get(scope.portal_key)
    if entry is None:
        return {
            "portal_key": scope.portal_key,
            "scope_key": scope.scope_key,
            "status": "unknown",
            "reason_code": "adapter_unverified",
            "checked_at": now_iso,
            "expires_at": expires_iso,
            "generation": browser.browser_generation,
            "stale_generation": False,
        }

    try:
        context = await browser.ensure_connected(timeout_ms=1500)
    except BrowserUnavailableError:
        return {
            "portal_key": scope.portal_key,
            "scope_key": scope.scope_key,
            "status": "disconnected",
            "reason_code": "browser_disconnected",
            "checked_at": now_iso,
            "expires_at": None,
            "generation": browser.browser_generation,
            "stale_generation": False,
        }

    probe_gen = browser.browser_generation
    allow_test_origins = browser.allow_test_origins
    target_page = page
    if target_page is not None and (
        target_page.is_closed()
        or not _url_matches_scope(
            target_page.url or "",
            entry,
            scope,
            allow_test_origins=allow_test_origins,
        )
    ):
        target_page = None

    if target_page is None:
        for cand in [p for p in context.pages if not p.is_closed()][:40]:
            if _url_matches_scope(
                cand.url or "",
                entry,
                scope,
                allow_test_origins=allow_test_origins,
            ):
                target_page = cand
                break

    if target_page is None:
        if entry.auth_model == "tenant_dependent" and scope.scope_key == "default" and not scope.entry_url:
            reason = "tenant_required"
        else:
            reason = "no_open_tab"
        return {
            "portal_key": scope.portal_key,
            "scope_key": scope.scope_key,
            "status": "unknown",
            "reason_code": reason,
            "checked_at": now_iso,
            "expires_at": expires_iso,
            "generation": probe_gen,
            "stale_generation": False,
        }

    status_val: PortalStatusLiteral = "unknown"
    reason_code: str | None = None
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            original_url = target_page.url or ""
            signals = await _evaluate_page_signals(target_page, entry)
            final_url = target_page.url or original_url
            if browser.browser_generation != probe_gen:
                return {
                    "portal_key": scope.portal_key,
                    "scope_key": scope.scope_key,
                    "status": "unknown",
                    "reason_code": "generation_changed",
                    "checked_at": now_iso,
                    "expires_at": None,
                    "generation": probe_gen,
                    "stale_generation": True,
                }
            status_val, reason_code = _classify_probe_signals(
                entry=entry,
                original_url=original_url,
                final_url=final_url,
                signals=signals,
            )
    except TimeoutError:
        status_val, reason_code = "unavailable", "timeout"
    except PlaywrightError:
        status_val, reason_code = "unavailable", "probe_error"

    stale = browser.browser_generation != probe_gen
    checked_done = datetime.now(UTC)
    return {
        "portal_key": scope.portal_key,
        "scope_key": scope.scope_key,
        "status": status_val,
        "reason_code": reason_code,
        "checked_at": checked_done.isoformat(),
        "expires_at": (checked_done + timedelta(seconds=SESSION_TTL_SECONDS)).isoformat()
        if not stale
        else None,
        "generation": probe_gen,
        "stale_generation": stale,
    }


def _discover_scopes(
    db: Session,
    *,
    user_id: str,
    browser: BrowserSession | None = None,
) -> dict[tuple[str, str], PortalScope]:
    scopes: dict[tuple[str, str], PortalScope] = {}
    for key, entry in PORTAL_REGISTRY.items():
        scopes[(key, entry.default_scope)] = PortalScope(
            portal_key=key,
            scope_key=entry.default_scope,
            entry_url=entry.login_url,
        )

    existing_rows = db.scalars(
        select(PortalSessionStatusRow).where(PortalSessionStatusRow.user_id == user_id)
    ).all()
    for row in existing_rows:
        if row.portal_key in PORTAL_REGISTRY:
            scopes.setdefault(
                (row.portal_key, row.scope_key),
                PortalScope(portal_key=row.portal_key, scope_key=row.scope_key),
            )

    allow_test = browser.allow_test_origins if browser is not None else set()
    urls: list[str] = []
    for j_url, a_url in db.execute(
        select(JobPosting.job_url, JobPosting.apply_url).limit(200)
    ):
        if a_url:
            urls.append(str(a_url))
        if j_url:
            urls.append(str(j_url))
    for app_job_url, app_apply_url in db.execute(
        select(JobPosting.job_url, JobPosting.apply_url)
        .join(Application, Application.job_id == JobPosting.id)
        .where(Application.user_id == user_id)
        .limit(100)
    ):
        if app_apply_url:
            urls.append(str(app_apply_url))
        if app_job_url:
            urls.append(str(app_job_url))

    if browser is not None and browser._context is not None and browser._browser is not None and browser._browser.is_connected():
        for page in [p for p in browser._context.pages if not p.is_closed()][:40]:
            if page.url:
                urls.append(page.url)

    for raw_url in urls:
        matched = _match_portal_for_url(raw_url, allow_test_origins=allow_test)
        if matched is None:
            continue
        p_key, s_key = matched
        origin = extract_origin(raw_url)
        if (p_key, s_key) not in scopes:
            scopes[(p_key, s_key)] = PortalScope(
                portal_key=p_key,
                scope_key=s_key,
                entry_url=f"{origin}/" if origin else None,
            )
    return scopes


def _invalidate_stuck_checking_rows(db: Session, *, user_id: str | None = None) -> None:
    now = datetime.now(UTC)
    stmt = select(PortalSessionStatusRow).where(PortalSessionStatusRow.status == "checking")
    if user_id is not None:
        stmt = stmt.where(PortalSessionStatusRow.user_id == user_id)
    rows = db.scalars(stmt).all()
    changed = False
    for row in rows:
        task_key = (row.user_id, row.portal_key, row.scope_key)
        task = _ACTIVE_PROBE_TASKS.get(task_key)
        if task is not None and not task.done():
            continue
        checked = parse_utc_rfc3339(row.checked_at) if row.checked_at else None
        if checked is None or (now - checked).total_seconds() >= CHECKING_STALE_SECONDS or task is None:
            row.status = "unknown"
            row.reason_code = "check_expired"
            row.browser_generation = None
            row.checked_at = utc_rfc3339()
            row.expires_at = None
            row.revision += 1
            changed = True
    if changed:
        db.commit()


def _serialize_portal_row(
    row: PortalSessionStatusRow,
    *,
    entry: PortalRegistryEntry,
    browser_connected: bool,
    now: datetime,
) -> dict[str, Any]:
    effective_status: PortalStatusLiteral = row.status  # type: ignore[assignment]
    reason_code = row.reason_code
    expired = False
    if row.expires_at:
        exp_dt = parse_utc_rfc3339(row.expires_at)
        expired = exp_dt <= now
    if not browser_connected and row.status != "checking":
        effective_status = "disconnected"
        reason_code = reason_code or "browser_disconnected"
    return {
        "portal_key": row.portal_key,
        "scope_key": row.scope_key,
        "name": entry.name,
        "auth_model": entry.auth_model,
        "discovery_capable": entry.discovery_capable,
        "status": effective_status,
        "reason_code": reason_code,
        "checked_at": row.checked_at,
        "expires_at": row.expires_at,
        "expired": expired,
        "revision": row.revision,
    }


def _ensure_portal_rows(
    db: Session, *, user_id: str, scopes: dict[tuple[str, str], PortalScope]
) -> dict[tuple[str, str], PortalSessionStatusRow]:
    existing = {
        (row.portal_key, row.scope_key): row
        for row in db.scalars(
            select(PortalSessionStatusRow).where(PortalSessionStatusRow.user_id == user_id)
        ).all()
    }
    added = False
    for (p_key, s_key), _scope in scopes.items():
        if (p_key, s_key) not in existing:
            row = PortalSessionStatusRow(
                user_id=user_id,
                portal_key=p_key,
                scope_key=s_key,
                status="unknown",
                reason_code=None,
                browser_generation=None,
                checked_at=None,
                expires_at=None,
                revision=1,
            )
            db.add(row)
            existing[(p_key, s_key)] = row
            added = True
    if added:
        db.commit()
    return existing


def count_fresh_preflight_issues(db: Session, *, user_id: str, now: datetime | None = None) -> tuple[int, int]:
    """Count fresh (non-expired) login_required scopes and challenge/unavailable blocked scopes."""
    ref = now or datetime.now(UTC)
    rows = db.scalars(
        select(PortalSessionStatusRow).where(
            PortalSessionStatusRow.user_id == user_id,
            PortalSessionStatusRow.status.in_(("login_required", "challenge", "unavailable")),
        )
    ).all()
    login_scopes: set[tuple[str, str]] = set()
    blocked_scopes: set[tuple[str, str]] = set()
    for row in rows:
        if not row.expires_at or parse_utc_rfc3339(row.expires_at) <= ref:
            continue
        key = (row.portal_key, row.scope_key)
        if row.status == "login_required":
            login_scopes.add(key)
        else:
            blocked_scopes.add(key)
    return len(login_scopes), len(blocked_scopes)


async def list_portal_sessions(
    db: Session,
    *,
    user_id: str,
    browser: BrowserSession,
) -> list[dict[str, Any]]:
    """Return all registered and discovered portal scope statuses cleanly from SQLite + browser state."""
    browser_connected = bool((await browser.status(timeout_ms=1000))["connected"])
    _invalidate_stuck_checking_rows(db, user_id=user_id)
    scopes = _discover_scopes(db, user_id=user_id, browser=browser if browser_connected else None)
    rows_map = _ensure_portal_rows(db, user_id=user_id, scopes=scopes)
    now = datetime.now(UTC)

    items: list[dict[str, Any]] = []
    for (p_key, _s_key), row in sorted(rows_map.items()):
        entry = PORTAL_REGISTRY.get(p_key)
        if entry is None:
            continue
        items.append(
            _serialize_portal_row(
                row,
                entry=entry,
                browser_connected=browser_connected,
                now=now,
            )
        )
    return items


async def _run_scope_probe_task(
    session_factory: sessionmaker[Session],
    *,
    user_id: str,
    scope: PortalScope,
    browser: BrowserSession,
    started_generation: int,
) -> None:
    task_key = (user_id, scope.portal_key, scope.scope_key)
    try:
        outcome = await probe_session(scope, browser)
        if outcome.get("stale_generation") or (
            started_generation > 0 and browser.browser_generation not in {started_generation, outcome.get("generation")}
        ):
            return
        with session_factory() as db:
            row = db.scalar(
                select(PortalSessionStatusRow).where(
                    PortalSessionStatusRow.user_id == user_id,
                    PortalSessionStatusRow.portal_key == scope.portal_key,
                    PortalSessionStatusRow.scope_key == scope.scope_key,
                )
            )
            if row is None:
                return
            status_str = str(outcome["status"])
            row.status = status_str
            row.reason_code = outcome["reason_code"]
            gen_val = outcome.get("generation")
            row.browser_generation = (
                str(gen_val if gen_val is not None else started_generation)
                if status_str in {"authenticated", "public_access", "login_required", "challenge"}
                else (str(gen_val) if gen_val is not None else None)
            )
            row.checked_at = outcome["checked_at"]
            row.expires_at = outcome["expires_at"]
            row.revision += 1
            db.commit()
    except asyncio.CancelledError:
        with suppress(Exception), session_factory() as db:
            row = db.scalar(
                select(PortalSessionStatusRow).where(
                    PortalSessionStatusRow.user_id == user_id,
                    PortalSessionStatusRow.portal_key == scope.portal_key,
                    PortalSessionStatusRow.scope_key == scope.scope_key,
                )
            )
            if row is not None and row.status == "checking":
                row.status = "unknown"
                row.reason_code = "check_cancelled"
                row.browser_generation = None
                row.checked_at = utc_rfc3339()
                row.expires_at = None
                row.revision += 1
                db.commit()
        raise
    finally:
        current = asyncio.current_task()
        if _ACTIVE_PROBE_TASKS.get(task_key) is current:
            _ACTIVE_PROBE_TASKS.pop(task_key, None)


async def start_portal_verification(
    db: Session,
    session_factory: sessionmaker[Session],
    *,
    user_id: str,
    browser: BrowserSession,
    targets: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Mark target scopes checking in SQLite and spawn one bounded probe task per scope."""
    available_scopes = _discover_scopes(db, user_id=user_id, browser=browser)
    selected: list[PortalScope] = []
    seen: set[tuple[str, str]] = set()

    if targets:
        for raw_target in targets:
            p_key = (raw_target.get("portal_key") or "").strip().lower()
            entry = PORTAL_REGISTRY.get(p_key)
            if entry is None:
                raise PreflightPortalError("unknown_portal", f"Portal '{p_key}' não reconhecido.", 422)
            s_key = (raw_target.get("scope_key") or entry.default_scope).strip().lower()
            if (p_key, s_key) in seen:
                continue
            seen.add((p_key, s_key))
            selected.append(
                available_scopes.get((p_key, s_key))
                or PortalScope(portal_key=p_key, scope_key=s_key, entry_url=entry.login_url)
            )
            available_scopes.setdefault((p_key, s_key), selected[-1])
    else:
        for _pair, sc in sorted(available_scopes.items()):
            selected.append(sc)

    rows_map = _ensure_portal_rows(db, user_id=user_id, scopes=available_scopes)
    now_iso = utc_rfc3339()
    response_items: list[dict[str, Any]] = []
    started_gen = browser.browser_generation

    for scope in selected:
        row = rows_map[(scope.portal_key, scope.scope_key)]
        row.status = "checking"
        row.reason_code = None
        row.browser_generation = str(started_gen) if started_gen else None
        row.checked_at = now_iso
        row.expires_at = None
        row.revision += 1

    db.commit()

    now_dt = datetime.now(UTC)
    for scope in selected:
        row = rows_map[(scope.portal_key, scope.scope_key)]
        entry = PORTAL_REGISTRY[scope.portal_key]
        task_key = (user_id, scope.portal_key, scope.scope_key)
        existing_task = _ACTIVE_PROBE_TASKS.get(task_key)
        if existing_task is None or existing_task.done():
            _ACTIVE_PROBE_TASKS[task_key] = asyncio.create_task(
                _run_scope_probe_task(
                    session_factory,
                    user_id=user_id,
                    scope=scope,
                    browser=browser,
                    started_generation=started_gen,
                )
            )
        response_items.append(
            _serialize_portal_row(
                row,
                entry=entry,
                browser_connected=True,
                now=now_dt,
            )
        )
    return response_items


def _resolve_login_url(
    db: Session,
    *,
    user_id: str,
    entry: PortalRegistryEntry,
    scope_key: str,
    allow_test_origins: set[str],
) -> str:
    if scope_key == entry.default_scope and entry.login_url:
        return entry.login_url

    if entry.auth_model == "tenant_dependent" and scope_key == "default":
        raise PreflightPortalError(
            "tenant_required",
            f"O portal {entry.name} depende do endereço da empresa (tenant). "
            "Abra ou salve uma vaga desse portal antes de iniciar o login.",
            status_code=422,
        )

    app_urls = list(
        db.scalars(
            select(JobPosting.apply_url)
            .join(Application, Application.job_id == JobPosting.id)
            .where(Application.user_id == user_id)
            .limit(100)
        ).all()
    )
    job_urls = list(db.scalars(select(JobPosting.apply_url).limit(200)).all())
    for cand_url in [*app_urls, *job_urls]:
        if not cand_url:
            continue
        matched = _match_portal_for_url(cand_url, allow_test_origins=allow_test_origins)
        if matched == (entry.portal_key, scope_key):
            return extract_origin(cand_url) + "/"

    if _host_matches_domain(scope_key.split(":")[0], entry.valid_domains):
        return f"https://{scope_key}/"

    if any(urlparse(origin).netloc.lower() == scope_key.lower() for origin in allow_test_origins):
        for origin in allow_test_origins:
            if urlparse(origin).netloc.lower() == scope_key.lower():
                return f"{origin}/"

    raise PreflightPortalError(
        "tenant_required",
        f"Não há URL validada para o escopo '{scope_key}' em {entry.name}.",
        status_code=422,
    )


async def open_portal_login(
    db: Session,
    *,
    user_id: str,
    browser: BrowserSession,
    portal_key: str,
    scope_key: str | None = None,
) -> dict[str, Any]:
    """Open the registry-defined or validated tenant login entry page in the attached Chrome."""
    clean_portal = portal_key.strip().lower()
    entry = PORTAL_REGISTRY.get(clean_portal)
    if entry is None:
        raise PreflightPortalError("unknown_portal", "Portal desconhecido.", status_code=404)

    resolved_scope = (scope_key or entry.default_scope).strip().lower()
    target_url = _resolve_login_url(
        db,
        user_id=user_id,
        entry=entry,
        scope_key=resolved_scope,
        allow_test_origins=browser.allow_test_origins,
    )
    try:
        context = await browser.ensure_connected(timeout_ms=1500)
        page = await context.new_page()
        await browser.safe_goto(page, target_url, timeout_ms=7000)
    except (BrowserUnavailableError, PlaywrightError, UnsafeURLError) as exc:
        raise PreflightPortalError(
            "browser_unavailable",
            "Não foi possível abrir a página de login do portal no Chrome.",
            status_code=503,
        ) from exc

    return {"opened": True, "portal_key": clean_portal, "scope_key": resolved_scope}


async def observe_portals(browser: BrowserSession, *, refresh: bool = False) -> list[dict[str, Any]]:
    """Backwards-compatible wrapper observing currently open portal tabs without DB context."""
    items: list[dict[str, Any]] = []
    for key, entry in PORTAL_REGISTRY.items():
        outcome = await probe_session(
            PortalScope(portal_key=key, scope_key=entry.default_scope, entry_url=entry.login_url),
            browser,
        )
        outcome["name"] = entry.name
        outcome["auth_model"] = entry.auth_model
        outcome["discovery_capable"] = entry.discovery_capable
        items.append(outcome)
    return items


async def cancel_all_probe_tasks() -> None:
    """Cancel active verification background tasks on shutdown."""
    tasks = list(_ACTIVE_PROBE_TASKS.values())
    _ACTIVE_PROBE_TASKS.clear()
    for task in tasks:
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task
