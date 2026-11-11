"""CareerPilot FastAPI application entrypoint with loopback CORS, SQLite WAL lifespan, and pt-BR dashboard."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from backend.app.api.routes import router as api_router
from backend.app.core.config import Settings, get_settings
from backend.app.core.logging import configure_logging
from backend.app.db.base import Base
from backend.app.db.session import get_engine, get_session_factory
from backend.app.models import tables as _models  # noqa: F401
from backend.app.workers.scheduler import DiscoveryScheduler

APP_DIR = Path(__file__).resolve().parent


def create_app(settings_override: Settings | None = None) -> FastAPI:
    """Create configured CareerPilot FastAPI instance."""
    settings = settings_override or get_settings()
    configure_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = get_engine(settings)
        Base.metadata.create_all(bind=engine)
        scheduler = DiscoveryScheduler(settings, get_session_factory(settings))
        scheduler.recover_stale_runs()
        app.state.scheduler = scheduler
        if settings.scheduler_enabled:
            await scheduler.start()
        try:
            yield
        finally:
            await scheduler.stop()

    app = FastAPI(
        title="CareerPilot MVP",
        version="0.1.0",
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
            context={
                "test_ats_url": settings.test_ats_url,
                "dry_run_default": settings.dry_run,
            },
        )

    return app


app = create_app()
