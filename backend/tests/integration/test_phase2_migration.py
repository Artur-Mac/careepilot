"""Phase 2 Alembic migration tests: b1d2e3f4a501 -> c2e4a6b8d002 data upgrade, cleanup, FK integrity, and downgrade refusal."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from alembic import command as alembic_command
from backend.app.db.migrations import (
    build_alembic_config,
    prepare_and_upgrade_database,
    verify_database_at_head,
)
from backend.app.db.session import build_engine


def _ts() -> str:
    return datetime.now(UTC).isoformat(sep=" ")


def _seed_b1d2e3f4a501_database(db_path: Path) -> None:
    """Create a Phase 1 revision (b1d2e3f4a501) SQLite DB and populate mixed legacy rows."""
    db_url = f"sqlite:///{db_path}"
    cfg = build_alembic_config(db_url)
    alembic_command.upgrade(cfg, "b1d2e3f4a501")

    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        now = _ts()

        conn.execute(
            "INSERT INTO employers (id, name, domain, ats_platform, board_token, is_fictional_test, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("emp-real", "Recife Real", "recife.example", "gupy", None, 0, now),
        )
        conn.execute(
            "INSERT INTO employers (id, name, domain, ats_platform, board_token, is_fictional_test, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("emp-fict", "Fictional Demo", None, "fictional_ats", "demo", 1, now),
        )

        jobs = [
            ("job-mocked-unused", "emp-fict", "https://boards.greenhouse.io/fict/1", "sig-m1", "greenhouse", "Fictional Demo", "Demo Job"),
            ("job-real-unused", "emp-real", "https://recife.gupy.io/jobs/1", "sig-r1", "gupy", "Recife Real", "Dev Python Jr"),
            ("job-legacy-with-app", "emp-real", "https://remotive.com/jobs/99", "sig-app", "remotive", "Remotive Co", "Backend Legacy"),
        ]
        for jid, eid, curl, sig, src, comp, title in jobs:
            conn.execute(
                """
                INSERT INTO job_postings (
                    id, employer_id, canonical_url, dedup_signature, source, source_job_id,
                    company, title, location, workplace_type, description, description_hash,
                    job_url, apply_url, posted_at, discovered_at, expires_at, is_expired,
                    apply_mode, source_attribution, injection_signals
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    jid, eid, curl, sig, src, jid, comp, title, "Recife, PE", "remote",
                    "Python SQL", "d" * 64, curl, curl, now, now, None, 0,
                    "assisted_manual_submit", src, json.dumps([]),
                ),
            )
            conn.execute(
                """
                INSERT INTO job_source_records (
                    id, job_id, source, provider_job_id, raw_url, attribution, is_mocked, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (f"jsr-{jid}", jid, src, jid, curl, src, 1 if src == "greenhouse" else 0, now),
            )

        conn.execute(
            "INSERT INTO job_requirements (id, job_id, kind, term, snippet) VALUES (?, ?, ?, ?, ?)",
            ("req-1", "job-real-unused", "required", "Python", "Python required"),
        )
        conn.execute(
            "INSERT INTO scheduler_runs (id, status, jobs_discovered, notes, started_at, finished_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("sched-1", "ok", 5, None, now, now),
        )
        conn.execute(
            "INSERT INTO source_connectors (id, name, kind, mode, rate_limit_rpm, enabled, last_status, total_fetched) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("conn-gh", "greenhouse", "ats_board", "live", 30, 1, "idle", 0),
        )
        conn.execute(
            "INSERT INTO applications (id, job_id, user_id, status, answers_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("app-hist", "job-legacy-with-app", "local-applicant", "done", "{}", now, now),
        )
        conn.commit()
    finally:
        conn.close()


def test_phase2_migration_cleanup_fk_integrity_and_downgrade_refusal(tmp_path: Path) -> None:
    db_path = tmp_path / "phase2_mig.db"
    db_url = f"sqlite:///{db_path}"
    _seed_b1d2e3f4a501_database(db_path)

    res = prepare_and_upgrade_database(db_url, backup_dir=tmp_path / "backups")
    assert res["action"] == "upgraded"
    assert res["head"] == "e4f6a8b0c204"
    assert Path(str(res["backup_path"])).exists()

    engine = build_engine(db_url)
    try:
        assert verify_database_at_head(engine) == "e4f6a8b0c204"
        tables = set(inspect(engine).get_table_names())
        assert "discovery_runs" in tables
        assert "job_dismissals" in tables
        assert "scheduler_runs" not in tables
        assert "job_requirements" not in tables

        job_cols = {c["name"] for c in inspect(engine).get_columns("job_postings")}
        assert "apply_mode" not in job_cols
        assert {"summary_text", "eligibility_json"}.issubset(job_cols)

        emp_cols = {c["name"] for c in inspect(engine).get_columns("employers")}
        assert "board_token" not in emp_cols
        assert "is_fictional_test" not in emp_cols

        jsr_cols = {c["name"] for c in inspect(engine).get_columns("job_source_records")}
        assert "is_mocked" not in jsr_cols
        assert "discovery_channel" in jsr_cols

        with engine.connect() as conn:
            assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []
            assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1

            remaining_jobs = {
                r[0] for r in conn.execute(text("SELECT id FROM job_postings")).fetchall()
            }
            assert "job-mocked-unused" not in remaining_jobs
            assert "job-real-unused" in remaining_jobs
            # Historical application job stays preserved even though old source is retired
            assert "job-legacy-with-app" in remaining_jobs

            connectors = {
                r[0] for r in conn.execute(text("SELECT name FROM source_connectors")).fetchall()
            }
            assert "greenhouse" not in connectors
    finally:
        engine.dispose()

    cfg = build_alembic_config(db_url)
    with pytest.raises(RuntimeError, match="Destructive downgrade"):
        alembic_command.downgrade(cfg, "b1d2e3f4a501")
