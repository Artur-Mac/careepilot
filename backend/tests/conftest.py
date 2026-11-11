"""Shared pytest fixtures for isolated SQLite WAL databases, authenticated API clients, and live loopback servers."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from backend.app.browser_automation.fake_ats import fake_ats_app
from backend.app.core.config import Settings
from backend.app.db.base import Base
from backend.app.db.session import get_db, get_engine, get_session_factory, reset_engine_cache
from backend.app.main import create_app


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _LiveUvicornThread:
    """Runs an ASGI app on a free loopback port inside a daemon thread for real Playwright E2E."""

    def __init__(self, app: object, host: str, port: int) -> None:
        config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.url = f"http://{host}:{port}"

    def __enter__(self) -> _LiveUvicornThread:
        self.thread.start()
        deadline = time.monotonic() + 8.0
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        if not self.server.started:
            raise RuntimeError(f"Servidor de teste não iniciou em {self.url}")
        return self

    def __exit__(self, *_: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5.0)


@pytest.fixture()
def isolated_settings(tmp_path: Path) -> Iterator[Settings]:
    """Provide isolated temporary SQLite WAL database & artifacts folder per test."""
    reset_engine_cache()
    db_path = tmp_path / "test_careerpilot.db"
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    cfg = Settings(
        database_url=f"sqlite:///{db_path}",
        artifacts_dir=str(artifacts_dir),
        careerpilot_passcode="test-passcode-123",
        careerpilot_session_secret="test-session-secret-xyz",
        dry_run=True,
        live_mode_enabled=False,
        test_ats_enabled=True,
        test_ats_url="http://127.0.0.1:8010",
    )
    yield cfg
    reset_engine_cache()


@pytest.fixture()
def client(isolated_settings: Settings) -> Iterator[TestClient]:
    """FastAPI TestClient bound to isolated database."""
    app = create_app(isolated_settings)
    engine = get_engine(isolated_settings)
    Base.metadata.create_all(bind=engine)
    session_factory = get_session_factory(isolated_settings)

    def _override_get_db() -> Iterator[object]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as tc:
        yield tc


@pytest.fixture()
def auth_headers(client: TestClient) -> dict[str, str]:
    """Unlock local session and return Bearer + CSRF headers for test mutations."""
    resp = client.post("/api/auth/unlock", json={"passcode": "test-passcode-123"})
    assert resp.status_code == 200
    data = resp.json()
    return {
        "Authorization": f"Bearer {data['access_token']}",
        "X-CSRF-Token": data["csrf_token"],
    }


@pytest.fixture()
def live_e2e_servers(tmp_path: Path) -> Iterator[tuple[str, str, Settings]]:
    """Spin up both the Fictional Local ATS and the CareerPilot app on separate loopback ports."""
    reset_engine_cache()
    ats_port = _find_free_port()
    app_port = _find_free_port()
    db_path = tmp_path / "e2e_careerpilot.db"
    artifacts_dir = tmp_path / "e2e_artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    cfg = Settings(
        careerpilot_port=app_port,
        database_url=f"sqlite:///{db_path}",
        artifacts_dir=str(artifacts_dir),
        careerpilot_passcode="demo-local-2026",
        careerpilot_session_secret="e2e-session-secret-xyz",
        dry_run=True,
        live_mode_enabled=False,
        test_ats_enabled=True,
        test_ats_url=f"http://127.0.0.1:{ats_port}",
    )
    app = create_app(cfg)
    engine = get_engine(cfg)
    Base.metadata.create_all(bind=engine)
    session_factory = get_session_factory(cfg)

    def _override_get_db() -> Iterator[object]:
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db

    with (
        _LiveUvicornThread(fake_ats_app, "127.0.0.1", ats_port) as ats_srv,
        _LiveUvicornThread(app, "127.0.0.1", app_port) as app_srv,
    ):
        yield app_srv.url, ats_srv.url, cfg

    reset_engine_cache()
