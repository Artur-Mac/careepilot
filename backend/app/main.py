"""CareerPilot FastAPI application entrypoint with loopback CORS, SQLite WAL lifespan, and pt-BR dashboard."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from backend.app.api.routes import router as api_router
from backend.app.browser_automation.session import BrowserSession
from backend.app.core.config import Settings, get_settings
from backend.app.core.logging import configure_logging
from backend.app.db.migrations import verify_database_at_head
from backend.app.db.session import get_engine, get_session_factory
from backend.app.models import tables as _models  # noqa: F401
from backend.app.services.discovery.service import recover_stale_discovery_runs
from backend.app.services.resume.service import cleanup_orphaned_temp_resumes

APP_DIR = Path(__file__).resolve().parent


def create_app(settings_override: Settings | None = None) -> FastAPI:
    """Create configured CareerPilot FastAPI instance."""
    settings = settings_override or get_settings()
    configure_logging()

    @asynccontextmanager
    async def lifespan(app_instance: FastAPI) -> AsyncIterator[None]:
        engine = get_engine(settings)
        verify_database_at_head(engine)
        cleanup_orphaned_temp_resumes(settings)

        sf = get_session_factory(settings)
        with sf() as db:
            recover_stale_discovery_runs(
                db, reason="Busca interrompida em execução anterior do servidor."
            )

        app_instance.state.browser_session = BrowserSession(settings)
        app_instance.state.discovery_tasks = {}
        try:
            yield
        finally:
            tasks: dict[str, asyncio.Task[None]] = getattr(
                app_instance.state, "discovery_tasks", {}
            )
            pending = [t for t in tasks.values() if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            tasks.clear()

            with sf() as db:
                recover_stale_discovery_runs(
                    db, reason="Busca interrompida pelo encerramento do servidor."
                )

            browser_session: BrowserSession | None = getattr(
                app_instance.state, "browser_session", None
            )
            if browser_session is not None:
                with suppress(Exception):
                    await browser_session.shutdown()

    app = FastAPI(
        title="CareerPilot",
        version="0.2.0",
        lifespan=lifespan,
    )
    app.state.settings = settings

    # Strict loopback-only CORS — never allow wildcard '*' origins on local user plane.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(settings.allowed_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", "Authorization", "X-CSRF-Token"],
    )

    static_dir = APP_DIR / "static"
    templates_dir = APP_DIR / "templates"
    static_dir.mkdir(parents=True, exist_ok=True)
    templates_dir.mkdir(parents=True, exist_ok=True)

    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    templates = Jinja2Templates(directory=str(templates_dir))

    app.include_router(api_router)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={},
        )

    return app


app = create_app()
