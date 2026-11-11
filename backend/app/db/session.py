"""Database engine, SQLite WAL configuration, and transactional session helpers."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from backend.app.core.config import Settings, get_settings


def _ensure_sqlite_parent(database_url: str) -> None:
    if database_url.startswith("sqlite:///") and not database_url.endswith(":memory:"):
        raw_path = database_url.removeprefix("sqlite:///")
        path = Path(raw_path)
        path.parent.mkdir(parents=True, exist_ok=True)


def build_engine(database_url: str) -> Engine:
    """Create SQLAlchemy engine with SQLite WAL mode and foreign key constraints enabled."""
    _ensure_sqlite_parent(database_url)
    is_sqlite = database_url.startswith("sqlite")
    connect_args: dict[str, Any] = {"check_same_thread": False, "timeout": 30} if is_sqlite else {}
    engine = create_engine(
        database_url,
        connect_args=connect_args,
        future=True,
        pool_pre_ping=True,
    )
    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA synchronous=NORMAL;")
            cursor.execute("PRAGMA foreign_keys=ON;")
            cursor.execute("PRAGMA busy_timeout=5000;")
            cursor.close()

    return engine


_engines: dict[str, Engine] = {}
_session_factories: dict[str, sessionmaker[Session]] = {}


def get_engine(settings: Settings | None = None) -> Engine:
    cfg = settings or get_settings()
    if cfg.database_url not in _engines:
        _engines[cfg.database_url] = build_engine(cfg.database_url)
    return _engines[cfg.database_url]


def get_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    cfg = settings or get_settings()
    if cfg.database_url not in _session_factories:
        _session_factories[cfg.database_url] = sessionmaker(
            bind=get_engine(cfg),
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )
    return _session_factories[cfg.database_url]


def reset_engine_cache() -> None:
    """Dispose cached engines (primarily for isolated test fixtures)."""
    for eng in _engines.values():
        eng.dispose()
    _engines.clear()
    _session_factories.clear()


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a scoped SQLAlchemy session."""
    factory = get_session_factory()
    db = factory()
    try:
        yield db
    finally:
        db.close()
