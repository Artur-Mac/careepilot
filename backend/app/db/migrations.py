"""Database backup, schema preflight verification, and Alembic upgrade helpers."""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, inspect

from alembic import command
from backend.app.core.config import get_settings
from backend.app.db.session import build_engine

INITIAL_REVISION = "adf094dd1fee"


class DatabaseMigrationError(RuntimeError):
    """Raised when a database is unmigrated, out of date, or fails schema checks."""


class SchemaMismatchError(DatabaseMigrationError):
    """Raised when an unversioned database differs from the initial Alembic revision."""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def build_alembic_config(database_url: str, *, connection: Any | None = None) -> Config:
    """Create an Alembic Config bound to the repository script directory and URL."""
    root = _repo_root()
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    cfg.attributes["database_url"] = database_url
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


def sqlite_path_from_url(database_url: str) -> Path | None:
    """Return the filesystem path for a non-memory SQLite URL, or None otherwise."""
    if not database_url.startswith("sqlite:///"):
        return None
    raw_path = unquote(database_url.removeprefix("sqlite:///").split("?", 1)[0])
    if raw_path in {"", ":memory:"}:
        return None
    return Path(raw_path).resolve()


def backup_sqlite_database(
    database_url: str,
    *,
    backup_dir: Path | None = None,
    reason: str = "pre-upgrade",
) -> Path | None:
    """Create a consistent SQLite backup using stdlib sqlite3.Connection.backup."""
    db_path = sqlite_path_from_url(database_url)
    if db_path is None or not db_path.exists() or db_path.stat().st_size == 0:
        return None

    target_dir = (backup_dir or (db_path.parent / "backups")).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    safe_reason = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in reason)
    backup_path = target_dir / f"{db_path.stem}-{stamp}-{safe_reason}{db_path.suffix or '.db'}"

    src_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    dst_conn = sqlite3.connect(str(backup_path))
    try:
        src_conn.backup(dst_conn)
        dst_conn.commit()
    finally:
        dst_conn.close()
        src_conn.close()
    with __import__("contextlib").suppress(OSError):
        os.chmod(backup_path, 0o600)
    return backup_path


def _snapshot_schema(engine: Engine) -> dict[str, dict[str, Any]]:
    insp = inspect(engine)
    snapshot: dict[str, dict[str, Any]] = {}
    for table_name in sorted(insp.get_table_names()):
        if table_name == "alembic_version" or table_name.startswith("sqlite_"):
            continue
        columns = {
            col["name"]: {
                "type": str(col["type"]).upper(),
                "nullable": bool(col["nullable"]),
            }
            for col in insp.get_columns(table_name)
        }
        pk_constraint = insp.get_pk_constraint(table_name)
        pk_cols = tuple(pk_constraint.get("constrained_columns") or ())
        fks = sorted(
            (
                tuple(fk.get("constrained_columns") or ()),
                str(fk.get("referred_table") or ""),
                tuple(fk.get("referred_columns") or ()),
            )
            for fk in insp.get_foreign_keys(table_name)
        )
        unique_indexes: set[tuple[tuple[str, ...], bool]] = set()
        for uq in insp.get_unique_constraints(table_name):
            cols = tuple(uq.get("column_names") or ())
            if cols:
                unique_indexes.add((cols, True))
        for idx in insp.get_indexes(table_name):
            cols = tuple(str(c) for c in (idx.get("column_names") or ()) if c)
            if cols:
                unique_indexes.add((cols, bool(idx.get("unique"))))
        snapshot[table_name] = {
            "columns": columns,
            "pk": pk_cols,
            "fks": fks,
            "indexes": sorted(unique_indexes),
        }
    return snapshot


def compare_schema_to_initial_revision(engine: Engine) -> list[str]:
    """Compare an existing database schema against the initial Alembic revision."""
    ref_engine = build_engine("sqlite:///:memory:")
    try:
        with ref_engine.connect() as ref_conn:
            cfg = build_alembic_config("sqlite:///:memory:", connection=ref_conn)
            command.upgrade(cfg, INITIAL_REVISION)
        expected = _snapshot_schema(ref_engine)
    finally:
        ref_engine.dispose()

    actual = _snapshot_schema(engine)
    diffs: list[str] = []

    missing_tables = sorted(set(expected) - set(actual))
    extra_tables = sorted(set(actual) - set(expected))
    if missing_tables:
        diffs.append(f"Missing tables vs {INITIAL_REVISION}: {', '.join(missing_tables)}")
    if extra_tables:
        diffs.append(f"Unexpected tables vs {INITIAL_REVISION}: {', '.join(extra_tables)}")

    for table in sorted(set(expected) & set(actual)):
        exp_t = expected[table]
        act_t = actual[table]
        exp_cols = exp_t["columns"]
        act_cols = act_t["columns"]
        missing_cols = sorted(set(exp_cols) - set(act_cols))
        extra_cols = sorted(set(act_cols) - set(exp_cols))
        if missing_cols:
            diffs.append(f"{table}: missing columns {', '.join(missing_cols)}")
        if extra_cols:
            diffs.append(f"{table}: unexpected columns {', '.join(extra_cols)}")
        for col in sorted(set(exp_cols) & set(act_cols)):
            if exp_cols[col] != act_cols[col]:
                diffs.append(
                    f"{table}.{col}: expected {exp_cols[col]}, got {act_cols[col]}"
                )
        if exp_t["pk"] != act_t["pk"]:
            diffs.append(f"{table}: PK expected {exp_t['pk']}, got {act_t['pk']}")
        if exp_t["fks"] != act_t["fks"]:
            diffs.append(f"{table}: FKs expected {exp_t['fks']}, got {act_t['fks']}")
        if exp_t["indexes"] != act_t["indexes"]:
            diffs.append(
                f"{table}: indexes/uniques expected {exp_t['indexes']}, got {act_t['indexes']}"
            )
    return diffs


def get_script_head(database_url: str = "sqlite:///:memory:") -> str:
    """Return the single Alembic script head revision ID."""
    cfg = build_alembic_config(database_url)
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    if len(heads) != 1:
        raise DatabaseMigrationError(f"Expected 1 Alembic head, found {heads}")
    return str(heads[0])


def verify_database_at_head(bind: Engine | Connection) -> str:
    """Verify the connected database has already been upgraded to Alembic head."""
    db_url = str(bind.engine.url) if isinstance(bind, Connection) else str(bind.url)
    head = get_script_head(db_url)
    if isinstance(bind, Connection):
        revs = MigrationContext.configure(bind).get_current_heads()
    else:
        with bind.connect() as conn:
            revs = MigrationContext.configure(conn).get_current_heads()
    if not revs:
        raise DatabaseMigrationError(
            "Database schema has no Alembic revision recorded. "
            "Run `uv run --locked python -m backend.app.db.migrations upgrade` before starting."
        )
    if set(revs) != {head}:
        raise DatabaseMigrationError(
            f"Database revision {revs} does not match expected head {head}. "
            "Run `uv run --locked python -m backend.app.db.migrations upgrade` before starting."
        )
    return head


def prepare_and_upgrade_database(
    database_url: str,
    *,
    backup_dir: Path | None = None,
) -> dict[str, Any]:
    """Safely upgrade a fresh, legacy create_all, or versioned database to Alembic head."""
    cfg = build_alembic_config(database_url)
    head = get_script_head(database_url)
    engine = build_engine(database_url)
    backup_path: Path | None = None
    action = "noop"

    try:
        with engine.connect() as conn:
            insp = inspect(conn)
            user_tables = [
                name
                for name in insp.get_table_names()
                if name != "alembic_version" and not name.startswith("sqlite_")
            ]
            current_revs = MigrationContext.configure(conn).get_current_heads()

        if not user_tables:
            action = "fresh_upgrade"
            command.upgrade(cfg, "head")
        elif not current_revs:
            backup_path = backup_sqlite_database(
                database_url,
                backup_dir=backup_dir,
                reason="pre-stamp-upgrade",
            )
            diffs = compare_schema_to_initial_revision(engine)
            if diffs:
                preview = "\n".join(f"- {item}" for item in diffs[:20])
                backup_note = f" Backup preserved at {backup_path}." if backup_path else ""
                raise SchemaMismatchError(
                    "Existing unversioned database does not match initial Alembic revision "
                    f"{INITIAL_REVISION}; refusing to stamp automatically.{backup_note}\n{preview}"
                )
            action = "stamped_and_upgraded"
            command.stamp(cfg, INITIAL_REVISION)
            command.upgrade(cfg, "head")
        elif set(current_revs) != {head}:
            backup_path = backup_sqlite_database(
                database_url,
                backup_dir=backup_dir,
                reason="pre-upgrade",
            )
            action = "upgraded"
            command.upgrade(cfg, "head")

        verified_head = verify_database_at_head(engine)
    finally:
        engine.dispose()

    return {
        "action": action,
        "head": verified_head,
        "backup_path": str(backup_path) if backup_path else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CareerPilot database migration helper")
    parser.add_argument("command", nargs="?", default="upgrade", choices=["upgrade", "check"])
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args(argv)

    database_url = args.database_url or get_settings().database_url
    if args.command == "check":
        engine = build_engine(database_url)
        try:
            head = verify_database_at_head(engine)
        finally:
            engine.dispose()
        print(f"Database is at head revision: {head}")
        return 0

    result = prepare_and_upgrade_database(database_url)
    print(
        f"Database {result['action']}; head={result['head']}"
        + (f"; backup={result['backup_path']}" if result["backup_path"] else "")
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DatabaseMigrationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
