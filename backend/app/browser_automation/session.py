"""CDP browser session manager for visible, signed-in Chrome/Chromium reuse."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any
from urllib.parse import urlparse

from playwright.async_api import (
    Browser,
    BrowserContext,
    Frame,
    Page,
    Playwright,
    Route,
    async_playwright,
)

from backend.app.browser_automation.adapters import is_compatible_application_origin
from backend.app.core.config import Settings
from backend.app.core.ssrf import (
    UnsafeURLError,
    extract_origin,
    validate_cdp_url,
    validate_external_url,
)


class BrowserUnavailableError(RuntimeError):
    """Raised when the local CDP Chrome instance cannot be reached."""


class TabNotFoundError(LookupError):
    """Raised when an opaque candidate tab identifier is unknown or no longer open."""


class BoundPageInvalidError(RuntimeError):
    """Raised when a bound application tab closed, moved off approved origin, or failed context checks."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _sanitize_tab_summary_url(raw_url: str) -> str:
    """Return scheme://host/path without query parameters or fragments that may hold tokens."""
    parsed = urlparse((raw_url or "").strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""
    port = f":{parsed.port}" if parsed.port else ""
    path = parsed.path or "/"
    return f"{parsed.scheme.lower()}://{parsed.hostname}{port}{path}"


def _matches_vacancy_hint(title: str, path_url: str, *, expected_title: str | None, expected_company: str | None) -> bool:
    """Best-effort local match check between a candidate tab and target vacancy title/company."""
    haystack = f"{title} {path_url}".lower()
    terms: list[str] = []
    for raw in (expected_company, expected_title):
        if not raw:
            continue
        for token in raw.lower().replace("/", " ").replace("-", " ").split():
            clean = token.strip(".,:;()[]")
            if len(clean) >= 3:
                terms.append(clean)
    if not terms:
        return True
    return any(term in haystack for term in terms[:6])


class BrowserSession:
    """Lifespan-owned CDP browser session connecting lazily to persistent signed-in Chrome.

    Ownership rules:
    - Discovery collectors borrow only CareerPilot-managed `discovery` tabs (closed cleanly
      after discovery or on server shutdown).
    - Application tabs (`app_id -> Page`) are bound for interactive user review and are NEVER
      auto-closed by shutdown or discovery cleanup.
    - Existing user Chrome tabs are never modified unless explicitly attached by user action.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        allow_test_origins: set[str] | None = None,
        max_discovery_tabs: int = 2,
    ) -> None:
        self.settings = settings
        self._allow_test_origins = {o.lower() for o in (allow_test_origins or set())}
        self._max_discovery_tabs = max(1, max_discovery_tabs)
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._discovery_pages: list[Page] = []
        self._discovery_semaphore = asyncio.Semaphore(self._max_discovery_tabs)
        self._discovery_pool_lock = asyncio.Lock()
        self._app_pages: dict[str, Page] = {}
        self._app_bound_origins: dict[str, str] = {}
        self._tab_registry: dict[str, tuple[Page, str]] = {}
        self._page_tab_ids: dict[int, str] = {}
        self._nav_gen_by_page_id: dict[int, int] = {}
        self._step_snapshots: dict[str, dict[str, Any]] = {}
        self._latest_step_id: dict[str, str] = {}
        self._guarded_page_ids: set[int] = set()
        self._connect_lock = asyncio.Lock()
        self._last_error: str | None = None

    @property
    def allow_test_origins(self) -> set[str]:
        return set(self._allow_test_origins)

    def _cdp_endpoint(self) -> str:
        return validate_cdp_url(self.settings.browser_cdp_url)

    def validate_target_url(self, url: str) -> str:
        """Validate a browser navigation target against SSRF/scheme rules."""
        return validate_external_url(
            url,
            allow_test_origins=self._allow_test_origins or None,
            resolve_dns=not bool(self._allow_test_origins),
        )

    async def ensure_connected(self, *, timeout_ms: int = 4000) -> BrowserContext:
        """Connect lazily over loopback CDP and reuse the persistent default browser context."""
        cdp_url = self._cdp_endpoint()
        async with self._connect_lock:
            if (
                self._browser is not None
                and self._browser.is_connected()
                and self._context is not None
            ):
                self._prune_closed_pages()
                return self._context

            pw: Playwright | None = self._playwright
            started_here = False
            try:
                if pw is None:
                    pw = await async_playwright().start()
                    started_here = True
                browser = await pw.chromium.connect_over_cdp(cdp_url, timeout=timeout_ms)
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                self._playwright = pw
                self._browser = browser
                self._context = context
                self._last_error = None
                self._prune_closed_pages()
                return context
            except Exception as exc:
                self._browser = None
                self._context = None
                if started_here and pw is not None:
                    with suppress(Exception):
                        await pw.stop()
                    self._playwright = None
                self._last_error = (
                    f"Navegador Chrome/Chromium não conectado em {cdp_url}. "
                    "Execute ./scripts/start_browser.sh e entre nas suas contas."
                )
                raise BrowserUnavailableError(self._last_error) from exc

    def _prune_closed_pages(self) -> None:
        self._discovery_pages = [p for p in self._discovery_pages if not p.is_closed()]
        stale_apps = [aid for aid, p in self._app_pages.items() if p.is_closed()]
        for aid in stale_apps:
            self._app_pages.pop(aid, None)
            self._app_bound_origins.pop(aid, None)
        stale_tab_ids = [tid for tid, (p, _) in self._tab_registry.items() if p.is_closed()]
        for tid in stale_tab_ids:
            p, _ = self._tab_registry.pop(tid)
            self._page_tab_ids.pop(id(p), None)
        stale_nav_ids = [
            pid
            for pid in self._nav_gen_by_page_id
            if pid not in {id(p) for p in self._app_pages.values()}
            and pid not in self._page_tab_ids
        ]
        for pid in stale_nav_ids:
            self._nav_gen_by_page_id.pop(pid, None)

    def get_navigation_generation(self, page: Page) -> int:
        """Return monotonic navigation generation counter observed on main frame."""
        return self._nav_gen_by_page_id.get(id(page), 1)

    async def _install_navigation_guard(self, page: Page) -> None:
        page_key = id(page)
        self._nav_gen_by_page_id.setdefault(page_key, 1)
        if page_key in self._guarded_page_ids:
            return
        self._guarded_page_ids.add(page_key)

        async def _guard_route(route: Route) -> None:
            req = route.request
            if req.is_navigation_request() and req.frame == page.main_frame:
                req_url = req.url
                if req_url == "about:blank":
                    await route.continue_()
                    return
                try:
                    validate_external_url(
                        req_url,
                        allow_test_origins=self._allow_test_origins or None,
                        resolve_dns=not bool(self._allow_test_origins),
                    )
                except UnsafeURLError:
                    await route.abort()
                    return
            await route.continue_()

        def _on_frame_navigated(frame: Frame) -> None:
            if frame != page.main_frame:
                return
            self._nav_gen_by_page_id[page_key] = self._nav_gen_by_page_id.get(page_key, 1) + 1
            current_url = frame.url or ""
            if (
                not current_url
                or current_url == "about:blank"
                or current_url.startswith("chrome-error://")
            ):
                return
            try:
                validate_external_url(
                    current_url,
                    allow_test_origins=self._allow_test_origins or None,
                    resolve_dns=not bool(self._allow_test_origins),
                )
            except UnsafeURLError:
                if not page.is_closed():
                    task = asyncio.create_task(page.goto("about:blank"))
                    task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

        await page.route("**/*", _guard_route)
        page.on("framenavigated", _on_frame_navigated)

    async def safe_goto(self, page: Page, url: str, *, timeout_ms: int = 25000) -> str:
        """Navigate a CareerPilot-managed tab only after SSRF/scheme validation and guard install."""
        safe_url = self.validate_target_url(url)
        await self._install_navigation_guard(page)
        await page.goto(safe_url, wait_until="domcontentloaded", timeout=timeout_ms)
        final_url = page.url or safe_url
        validate_external_url(
            final_url,
            allow_test_origins=self._allow_test_origins or None,
            resolve_dns=not bool(self._allow_test_origins),
        )
        return final_url

    @asynccontextmanager
    async def discovery_page(self) -> AsyncIterator[Page]:
        """Borrow a bounded reusable CareerPilot discovery tab from the persistent context."""
        context = await self.ensure_connected()
        await self._discovery_semaphore.acquire()
        page: Page | None = None
        had_error = False
        try:
            async with self._discovery_pool_lock:
                self._prune_closed_pages()
                for cand in self._discovery_pages:
                    if (
                        not cand.is_closed()
                        and not getattr(cand, "_cp_busy", False)
                        and not (cand.url or "").startswith("chrome-error://")
                    ):
                        cand._cp_busy = True  # type: ignore[attr-defined]
                        page = cand
                        break
                if page is None:
                    page = await context.new_page()
                    page._cp_busy = True  # type: ignore[attr-defined]
                    self._discovery_pages.append(page)
            await self._install_navigation_guard(page)
            yield page
        except Exception:
            had_error = True
            raise
        finally:
            if page is not None:
                page._cp_busy = False  # type: ignore[attr-defined]
                if had_error or (not page.is_closed() and (page.url or "").startswith("chrome-error://")):
                    async with self._discovery_pool_lock:
                        self._discovery_pages = [p for p in self._discovery_pages if p is not page]
                    if not page.is_closed():
                        with suppress(Exception):
                            await page.close()
            self._discovery_semaphore.release()

    async def close_discovery_tabs(self) -> None:
        """Close only tabs created for discovery; preserve application and user tabs."""
        async with self._discovery_pool_lock:
            pages = list(self._discovery_pages)
            self._discovery_pages.clear()
        for page in pages:
            if not page.is_closed():
                with suppress(Exception):
                    await page.close()

    def get_application_page(self, application_id: str) -> Page | None:
        """Return currently bound live tab for an application, or None."""
        self._prune_closed_pages()
        page = self._app_pages.get(application_id)
        if page is not None and not page.is_closed():
            return page
        self._app_pages.pop(application_id, None)
        self._app_bound_origins.pop(application_id, None)
        return None

    def detach_application_page(self, application_id: str) -> None:
        """Drop CareerPilot's tab binding without closing the user's tab or portal draft."""
        self._app_pages.pop(application_id, None)
        self._app_bound_origins.pop(application_id, None)

    def validate_bound_page(
        self, application_id: str, *, job_url: str | None = None
    ) -> Page:
        """Confirm bound tab is alive and remains on a compatible approved public origin."""
        page = self.get_application_page(application_id)
        if page is None:
            raise BoundPageInvalidError(
                "lost_closed",
                "Nenhuma aba vinculada ou a aba anterior foi fechada. "
                "Use Reabrir no navegador ou Anexar aba aberta.",
            )
        raw_url = page.url or ""
        try:
            validate_external_url(
                raw_url,
                allow_test_origins=self._allow_test_origins or None,
                resolve_dns=not bool(self._allow_test_origins),
            )
        except UnsafeURLError as exc:
            self.detach_application_page(application_id)
            raise BoundPageInvalidError(
                "unsafe_origin",
                "A aba vinculada navegou para uma origem insegura ou interna e foi desvinculada. "
                "Use Reabrir no navegador ou Anexar aba aberta.",
            ) from exc

        current_origin = extract_origin(raw_url)
        bound_origin = self._app_bound_origins.get(application_id, "")
        job_origin = extract_origin(job_url) if job_url else ""
        if bound_origin and not is_compatible_application_origin(
            bound_origin=bound_origin,
            current_origin=current_origin,
            job_origin=job_origin or None,
        ):
            self.detach_application_page(application_id)
            raise BoundPageInvalidError(
                "origin_changed",
                "A aba vinculada saiu da origem aprovada da candidatura e foi desvinculada. "
                "Seu rascunho no portal foi preservado; use Anexar aba aberta ou Reabrir no navegador.",
            )
        return page

    async def open_application_page(self, application_id: str, url: str) -> Page:
        """Open (or focus without re-navigating) a persistent Chrome tab bound to application_id."""
        safe_url = self.validate_target_url(url)
        await self.ensure_connected()
        if self.get_application_page(application_id) is not None:
            try:
                existing = self.validate_bound_page(application_id, job_url=safe_url)
                with suppress(Exception):
                    await existing.bring_to_front()
                return existing
            except BoundPageInvalidError:
                self.detach_application_page(application_id)

        context = await self.ensure_connected()
        page = await context.new_page()
        await self._install_navigation_guard(page)
        await page.goto(safe_url, wait_until="domcontentloaded", timeout=25000)
        final_url = page.url or safe_url
        validate_external_url(
            final_url,
            allow_test_origins=self._allow_test_origins or None,
            resolve_dns=not bool(self._allow_test_origins),
        )
        self._app_pages[application_id] = page
        self._app_bound_origins[application_id] = extract_origin(final_url)
        return page

    async def list_open_pages(
        self,
        *,
        application_id: str | None = None,
        expected_title: str | None = None,
        expected_company: str | None = None,
    ) -> list[dict[str, Any]]:
        """List safe attachable HTTP(S) tabs via temporary opaque server-generated IDs."""
        context = await self.ensure_connected()
        self._prune_closed_pages()
        discovery_set = {id(p) for p in self._discovery_pages}
        bound_reverse = {id(p): aid for aid, p in self._app_pages.items() if not p.is_closed()}
        results: list[dict[str, Any]] = []
        for page in context.pages:
            if page.is_closed() or id(page) in discovery_set:
                continue
            raw_url = page.url or ""
            try:
                validate_external_url(
                    raw_url,
                    allow_test_origins=self._allow_test_origins or None,
                    resolve_dns=False,
                )
            except UnsafeURLError:
                continue
            summary_url = _sanitize_tab_summary_url(raw_url)
            if not summary_url:
                continue
            origin = extract_origin(raw_url)
            pid = id(page)
            tab_id = self._page_tab_ids.get(pid)
            if not tab_id:
                tab_id = f"tab-{secrets.token_urlsafe(9)}"
                self._page_tab_ids[pid] = tab_id
            self._tab_registry[tab_id] = (page, origin)

            title = ""
            with suppress(Exception):
                title = (await page.title())[:120]
            bound_aid = bound_reverse.get(pid)
            results.append(
                {
                    "tab_id": tab_id,
                    "origin": origin,
                    "title": title or summary_url,
                    "url": summary_url,
                    "bound_application_id": bound_aid,
                    "is_bound_here": bool(application_id and bound_aid == application_id),
                    "matches_vacancy": _matches_vacancy_hint(
                        title,
                        summary_url,
                        expected_title=expected_title,
                        expected_company=expected_company,
                    ),
                }
            )
        return results

    async def attach_application_page(self, application_id: str, *, tab_id: str) -> Page:
        """Bind an existing safe Chrome tab identified only by server-generated opaque tab_id."""
        await self.ensure_connected()
        self._prune_closed_pages()
        record = self._tab_registry.get(tab_id)
        if record is None:
            raise TabNotFoundError("Aba selecionada não encontrada ou expirada. Atualize a lista de abas.")
        candidate, listed_origin = record
        if candidate.is_closed():
            self._tab_registry.pop(tab_id, None)
            self._page_tab_ids.pop(id(candidate), None)
            raise BoundPageInvalidError(
                "lost_closed",
                "A aba escolhida foi fechada antes de vincular. Abra ou liste as abas novamente.",
            )

        raw_url = candidate.url or ""
        try:
            validate_external_url(
                raw_url,
                allow_test_origins=self._allow_test_origins or None,
                resolve_dns=not bool(self._allow_test_origins),
            )
        except UnsafeURLError as exc:
            raise BoundPageInvalidError(
                "unsafe_origin",
                "A aba escolhida está em uma origem não pública ou insegura.",
            ) from exc

        current_origin = extract_origin(raw_url)
        if listed_origin and current_origin != listed_origin:
            raise BoundPageInvalidError(
                "origin_changed",
                "A aba mudou de origem desde que foi listada. Atualize as abas e confirme a seleção.",
            )

        await self._install_navigation_guard(candidate)
        self._app_pages[application_id] = candidate
        self._app_bound_origins[application_id] = current_origin
        with suppress(Exception):
            await candidate.bring_to_front()
        return candidate

    def store_step_snapshot(self, application_id: str, snapshot: Any) -> None:
        """Keep a bounded in-memory history of server-side step snapshots per application."""
        app_map = self._step_snapshots.setdefault(application_id, {})
        step_id = str(snapshot.step_id)
        app_map[step_id] = snapshot
        self._latest_step_id[application_id] = step_id
        while len(app_map) > 5:
            oldest = next(iter(app_map))
            if oldest == step_id:
                break
            app_map.pop(oldest, None)

    def get_step_snapshot(self, application_id: str, step_id: str) -> Any | None:
        return self._step_snapshots.get(application_id, {}).get(step_id)

    def get_latest_snapshot(self, application_id: str) -> Any | None:
        step_id = self._latest_step_id.get(application_id)
        if not step_id:
            return None
        return self.get_step_snapshot(application_id, step_id)

    async def status(self) -> dict[str, Any]:
        """Return safe CDP connection diagnostics without failing if Chrome is closed."""
        cdp_url = self._cdp_endpoint()
        connected = False
        open_tabs = 0
        error: str | None = None

        try:
            context = await self.ensure_connected(timeout_ms=1500)
            connected = True
            self._prune_closed_pages()
            open_tabs = sum(1 for p in context.pages if not p.is_closed())
        except BrowserUnavailableError as exc:
            error = str(exc)

        return {
            "connected": connected,
            "mode": "cdp_connected" if connected else "disconnected",
            "browser_url": cdp_url,
            "cdp_endpoint": cdp_url,
            "open_tabs": open_tabs,
            "discovery_tabs": len([p for p in self._discovery_pages if not p.is_closed()]),
            "tracked_applications": sum(1 for p in self._app_pages.values() if not p.is_closed()),
            "launch_command": "./scripts/start_browser.sh",
            "error": error,
        }

    async def shutdown(self) -> None:
        """Close CareerPilot discovery tabs and release CDP driver without closing Chrome or user tabs."""
        await self.close_discovery_tabs()
        self._app_pages.clear()
        self._app_bound_origins.clear()
        self._tab_registry.clear()
        self._page_tab_ids.clear()
        self._nav_gen_by_page_id.clear()
        self._step_snapshots.clear()
        self._latest_step_id.clear()
        self._guarded_page_ids.clear()
        self._browser = None
        self._context = None
        if self._playwright is not None:
            with suppress(Exception):
                await self._playwright.stop()
            self._playwright = None
