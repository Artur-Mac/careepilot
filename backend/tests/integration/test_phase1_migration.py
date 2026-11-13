"""Phase 1 Alembic migration tests: fresh upgrade, populated backfill, idempotency, backup/downgrade, and legacy stamp."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from alembic import command
from backend.app.core.config import Settings
from backend.app.db.migrations import (
    SchemaMismatchError,
    build_alembic_config,
    prepare_and_upgrade_database,
    verify_database_at_head,
)
from backend.app.db.session import build_engine, get_session_factory
from backend.app.schemas.profile import MasterProfileDocument


def _ts(offset_seconds: int = 0) -> str:
    return (datetime(2026, 11, 10, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=offset_seconds)).isoformat()


def _seed_adf094dd1fee_database(db_path: Path) -> None:
    """Create an adf094dd1fee database populated with real, unverified, conflicting, and fictional rows."""
    db_url = f"sqlite:///{db_path}"
    cfg = build_alembic_config(db_url)
    command.upgrade(cfg, "adf094dd1fee")

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        # User settings: user-a has explicit non-default prefs; user-b has unestablished defaults
        conn.execute(
            """
            INSERT INTO user_settings (
                id, user_id, target_roles, preferred_locations, workplace_preferences,
                seniority_preferences, contract_types, languages, excluded_companies,
                salary_floor, salary_currency, travel_tolerance, availability,
                work_authorization, dry_run, live_mode_enabled, notification_channel,
                scheduler_enabled, scheduler_interval_hours, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "us-a",
                "user-a",
                json.dumps(["Engenheira de Dados", "Backend Python"]),
                json.dumps(["Recife, PE"]),
                json.dumps(["remote", "hybrid"]),
                json.dumps(["junior"]),
                json.dumps(["clt"]),
                json.dumps(["pt-BR", "en"]),
                json.dumps(["Ruim Ltda"]),
                6500.0,
                "BRL",
                "none",
                "em 30 dias",
                "CLT Brasil e remoto exterior",
                1,
                0,
                "local_outbox",
                0,
                8,
                _ts(0),
            ),
        )
        conn.execute(
            """
            INSERT INTO user_settings (
                id, user_id, target_roles, preferred_locations, workplace_preferences,
                seniority_preferences, contract_types, languages, excluded_companies,
                salary_floor, salary_currency, travel_tolerance, availability,
                work_authorization, dry_run, live_mode_enabled, notification_channel,
                scheduler_enabled, scheduler_interval_hours, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "us-b",
                "user-b",
                json.dumps([]),
                json.dumps([]),
                json.dumps([]),
                json.dumps([]),
                json.dumps([]),
                json.dumps([]),
                json.dumps([]),
                None,
                "BRL",
                "none",
                "imediata",
                "Brasil (autorizado)",
                1,
                0,
                "local_outbox",
                0,
                8,
                _ts(0),
            ),
        )

        # Profile facts for user-a: verified + unverified + conflicting singletons with tie + fictional demo row
        facts = [
            # Full name verified
            ("pf-name", "user-a", "contact", "full_name", "Marina Silva", "public", 1, _ts(1), 0),
            # Email conflict at same timestamp (pf-email-z wins tie over pf-email-a!)
            ("pf-email-a", "user-a", "contact", "email", "old@example.org", "contact", 1, _ts(2), 0),
            ("pf-email-z", "user-a", "contact", "email", "new@example.org", "contact", 0, _ts(2), 0),
            # Unverified experience & skill & project & education & language + portfolio
            ("pf-port", "user-a", "contact", "portfolio", "https://marina.dev.example", "public", 0, _ts(3), 0),
            ("pf-exp-1", "user-a", "experience", "exp:acme", "Engenheira Python na Acme (2023-2025)", "public", 0, _ts(4), 0),
            ("pf-edu-1", "user-a", "education", "edu:ufpe", "Bacharelado Ciência da Computação UFPE", "public", 1, _ts(5), 0),
            ("pf-prj-1", "user-a", "project", "prj:etl", "Pipeline ETL Recife", "public", 0, _ts(6), 0),
            ("pf-skl-1", "user-a", "skill", "skill:python", "Python", "public", 0, _ts(7), 0),
            ("pf-lng-1", "user-a", "language", "lang:en", "Inglês Avançado", "public", 1, _ts(8), 0),
            # Fictional demo row that MUST be excluded
            ("pf-demo", "user-a", "skill", "skill:demo", "DemoShouldBeDropped", "public", 1, _ts(9), 1),
            # Real claim for user-b without availability/work_authorization fact (must remain blank in preferences)
            ("pf-b-name", "user-b", "contact", "full_name", "Lucas Recife", "public", 1, _ts(1), 0),
            ("pf-b-skl", "user-b", "skill", "skill:sql", "SQL", "public", 1, _ts(2), 0),
        ]
        for fid, uid, cat, key, val, sens, ver, c_at, demo in facts:
            conn.execute(
                """
                INSERT INTO profile_facts (
                    id, user_id, category, key, value, metadata_json,
                    sensitivity, user_verified, confidence, is_fictional_demo, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (fid, uid, cat, key, val, json.dumps({"company": "Acme"} if cat == "experience" else {}), sens, ver, 0.9, demo, c_at),
            )

        conn.execute(
            """
            INSERT INTO fact_evidence (
                id, fact_id, document_name, section_or_page, supporting_snippet, source_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("ev-1", "pf-exp-1", "cv_marina.pdf", "Página 1", "Engenheira Python na Acme", "a" * 64, _ts(10)),
        )

        # Employers & jobs: one explicit fictional_ats, one Employer.is_fictional_test=1,
        # AND one REAL employer whose name contains "fictíci" substring (must be retained!).
        conn.execute(
            "INSERT INTO employers (id, name, ats_platform, is_fictional_test, created_at) VALUES (?, ?, ?, ?, ?)",
            ("emp-real-fict-sub", "Consultoria Fictícia Real Ltda", "greenhouse", 0, _ts(0)),
        )
        conn.execute(
            "INSERT INTO employers (id, name, ats_platform, is_fictional_test, created_at) VALUES (?, ?, ?, ?, ?)",
            ("emp-fict-ats", "Fictional Demo Employer", "fictional_ats", 1, _ts(0)),
        )
        conn.execute(
            "INSERT INTO employers (id, name, ats_platform, is_fictional_test, created_at) VALUES (?, ?, ?, ?, ?)",
            ("emp-real", "Porto Labs", "lever", 0, _ts(0)),
        )

        jobs = [
            ("job-real-1", "emp-real", "https://jobs.lever.co/porto/1", "sig-1", "lever", "Porto Labs", "Data Engineer", _ts(1)),
            ("job-real-sub", "emp-real-fict-sub", "https://boards.greenhouse.io/fictreal/2", "sig-2", "greenhouse", "Consultoria Fictícia Real Ltda", "Backend Python", _ts(2)),
            ("job-fict-1", "emp-fict-ats", "http://127.0.0.1:8010/jobs/single-step", "sig-f", "fictional_ats", "Fictional Demo Employer", "Demo Single", _ts(3)),
        ]
        for jid, eid, curl, sig, src, comp, title, d_at in jobs:
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
                    "Python e SQL", "d" * 64, curl, curl, d_at, d_at, None, 0,
                    "assisted_manual_submit", src, json.dumps([]),
                ),
            )
            conn.execute(
                """
                INSERT INTO job_source_records (
                    id, job_id, source, provider_job_id, raw_url, attribution, is_mocked, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (f"jsr-{jid}", jid, src, jid, curl, src, 1 if src == "fictional_ats" else 0, d_at),
            )

        # Real ResumeVersion & AnswerVersion
        conn.execute(
            """
            INSERT INTO resume_versions (
                id, job_id, user_id, pdf_path, docx_path, pdf_sha256, extracted_text,
                structured_json, provenance_report, diff_summary, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "rv-real-1", "job-real-1", "user-a", "/tmp/old-resume.pdf", None, "b" * 64,
                "Marina Silva Python", json.dumps({"ok": True}), json.dumps([]), json.dumps({}), _ts(11),
            ),
        )
        conn.execute(
            """
            INSERT INTO answer_versions (
                id, job_id, user_id, answers_json, pending_questions, answers_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("av-1", "job-real-1", "user-a", json.dumps({"note": "old"}), json.dumps([]), "c" * 64, _ts(12)),
        )

        # Multiple historical applications on the SAME real job + varying old statuses
        apps = [
            ("app-verified", "job-real-1", "user-a", "verified", "rv-real-1", "av-1", "REC-1", _ts(10)),
            ("app-unver", "job-real-1", "user-a", "submitted_unverified", "rv-real-1", None, None, _ts(11)),
            ("app-review", "job-real-sub", "user-a", "ready_for_manual_review", None, None, None, _ts(12)),
            ("app-blocked", "job-real-sub", "user-a", "blocked", None, None, None, _ts(13)),
            ("app-need", "job-real-1", "user-a", "needs_user_input", None, None, None, _ts(14)),
            ("app-rej", "job-real-1", "user-a", "rejected", None, None, None, _ts(15)),
            ("app-await", "job-real-1", "user-a", "awaiting_approval", None, None, None, _ts(16)),
            ("app-fict", "job-fict-1", "user-a", "verified", None, None, "REC-F", _ts(17)),
        ]
        for aid, jid, uid, st, rvid, avid, rec, ts in apps:
            conn.execute(
                """
                INSERT INTO applications (
                    id, job_id, user_id, status, version, resume_version_id, answer_version_id,
                    confirmation_receipt_id, confirmation_url, confirmed_at, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (aid, jid, uid, st, 1, rvid, avid, rec, None, ts if rec else None, None, ts, ts),
            )

        # Approval & attempt & question child rows
        conn.execute(
            """
            INSERT INTO approval_requests (
                id, application_id, job_id, user_id, employer_name, canonical_apply_url,
                policy_mode, description_sha256, resume_sha256, answers_sha256, packet_hash,
                completeness_report, missing_items, fit_reasons, nonce, expires_at,
                consumed, consumed_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "apr-1", "app-await", "job-real-1", "user-a", "Porto Labs",
                "https://jobs.lever.co/porto/1", "assisted_manual_submit",
                "d" * 64, "b" * 64, "c" * 64, "e" * 64, "{}", "[]", "[]", "n1", _ts(200), 0, None, _ts(16),
            ),
        )
        conn.execute(
            """
            INSERT INTO approval_decisions (
                id, approval_request_id, application_id, decision, decided_by, comment, decided_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("apd-1", "apr-1", "app-await", "approved", "user-a", None, _ts(17)),
        )
        conn.execute(
            """
            INSERT INTO application_attempts (
                id, application_id, approval_request_id, dry_run, mode, status, receipt_id, error_message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("att-1", "app-await", "apr-1", 1, "assisted", "prepared", None, None, _ts(18)),
        )
        conn.execute(
            """
            INSERT INTO browser_attempts (
                id, application_attempt_id, target_url, final_url, status,
                fields_detected, fields_filled, missing_fields, evidence, confirmed_submission, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("ba-1", "att-1", "https://jobs.lever.co/porto/1", "https://jobs.lever.co/porto/1", "ok", "[]", "[]", "[]", "{}", 0, _ts(19)),
        )
        conn.execute(
            """
            INSERT INTO question_requests (
                id, application_id, field_key, question_label, reason, user_answer, resolved, created_at, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("qr-resolved", "app-need", "salary_expectation", "Pretensão salarial?", "required", "R$ 8.500", 1, _ts(20), _ts(21)),
        )
        conn.execute(
            """
            INSERT INTO question_requests (
                id, application_id, field_key, question_label, reason, user_answer, resolved, created_at, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("qr-open", "app-need", "notice_period", "Aviso prévio?", "required", None, 0, _ts(22), None),
        )
        conn.commit()
    finally:
        conn.close()


def test_phase1_fresh_upgrade_idempotency_and_zero_drift(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh.db"
    db_url = f"sqlite:///{db_path}"

    first = prepare_and_upgrade_database(db_url)
    assert first["action"] == "fresh_upgrade"
    assert first["head"] == "c2e4a6b8d002"

    second = prepare_and_upgrade_database(db_url)
    assert second["action"] == "noop"

    engine = build_engine(db_url)
    try:
        assert verify_database_at_head(engine) == "c2e4a6b8d002"
        tables = set(inspect(engine).get_table_names())
        assert "master_profiles" in tables
        assert "discovery_runs" in tables
        assert "job_dismissals" in tables
        for retired in (
            "approval_requests",
            "approval_decisions",
            "browser_attempts",
            "application_attempts",
            "answer_versions",
            "match_evaluations",
            "profile_facts",
            "fact_evidence",
            "question_requests",
            "scheduler_runs",
            "job_requirements",
        ):
            assert retired not in tables
    finally:
        engine.dispose()


def test_phase1_populated_backfill_fictional_cleanup_backup_and_downgrade_refusal(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "populated.db"
    db_url = f"sqlite:///{db_path}"
    _seed_adf094dd1fee_database(db_path)

    res = prepare_and_upgrade_database(db_url, backup_dir=tmp_path / "backups")
    assert res["action"] == "upgraded"
    assert res["head"] == "c2e4a6b8d002"
    backup_file = Path(str(res["backup_path"]))
    assert backup_file.exists()

    engine = build_engine(db_url)
    try:
        with engine.connect() as conn:
            fk_issues = conn.execute(text("PRAGMA foreign_key_check")).fetchall()
            assert fk_issues == []
            assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1

            # Verify fictional job dropped ONLY by source/is_fictional_test, NOT substring in real employer name
            job_ids = {
                row[0] for row in conn.execute(text("SELECT id FROM job_postings")).fetchall()
            }
            assert job_ids == {"job-real-1", "job-real-sub"}

            # Verify historical apps remapped & duplicate real apps on same job preserved
            app_rows = {
                row[0]: (row[1], row[2])
                for row in conn.execute(
                    text("SELECT id, status, answers_json FROM applications")
                ).fetchall()
            }
            assert "app-fict" not in app_rows
            assert app_rows["app-verified"][0] == "done"
            assert app_rows["app-unver"][0] == "done"
            assert app_rows["app-review"][0] == "review"
            assert app_rows["app-blocked"][0] == "needs_user"
            assert app_rows["app-need"][0] == "needs_user"
            assert app_rows["app-rej"][0] == "closed"
            assert app_rows["app-await"][0] == "ready"
            assert json.loads(app_rows["app-need"][1]) == {"salary_expectation": "R$ 8.500"}

            # Verify real ResumeVersion preserved
            assert conn.execute(text("SELECT id FROM resume_versions")).scalar() == "rv-real-1"
    finally:
        engine.dispose()

    # Verify MasterProfile documents for user-a and user-b via ORM
    sf = get_session_factory(Settings(database_url=db_url, careerpilot_passcode="test"))
    with sf() as db:
        from backend.app.models.tables import MasterProfile

        prof_a = db.get(MasterProfile, "user-a")
        prof_b = db.get(MasterProfile, "user-b")
        assert prof_a is not None and prof_b is not None

        doc_a = MasterProfileDocument.model_validate(prof_a.document)
        assert doc_a.contact.name == "Marina Silva"
        # Tie on (created_at, id): pf-email-z > pf-email-a
        assert doc_a.contact.email == "new@example.org"
        assert any(
            imp.kind == "migrated_note" and "old@example.org" in imp.extracted_text
            for imp in doc_a.imports
        )
        assert doc_a.contact.links.portfolio == "https://marina.dev.example"
        # Unverified real facts must still be preserved with original IDs and evidence
        assert [e.id for e in doc_a.experience] == ["pf-exp-1"]
        assert doc_a.experience[0].evidence[0].document_name == "cv_marina.pdf"
        assert [s.id for s in doc_a.skills] == ["pf-skl-1"]
        assert all(s.name != "DemoShouldBeDropped" for s in doc_a.skills)
        # User-a explicit settings copied; resolved question_requests NEVER promoted to standard_answers
        assert doc_a.preferences.availability == "em 30 dias"
        assert doc_a.preferences.work_authorization == "CLT Brasil e remoto exterior"
        assert doc_a.standard_answers == []

        # User-b unestablished defaults ("imediata", "Brasil") must remain blank!
        doc_b = MasterProfileDocument.model_validate(prof_b.document)
        assert doc_b.preferences.availability is None
        assert doc_b.preferences.work_authorization is None

    # Destructive downgrade must fail loudly referencing backup
    with pytest.raises(RuntimeError, match="Destructive downgrade from Phase"):
        command.downgrade(build_alembic_config(db_url), "adf094dd1fee")


def test_legacy_unversioned_create_all_stamp_and_mismatch_refusal(tmp_path: Path) -> None:
    # 1. Legacy unversioned DB matching adf094dd1fee -> stamped & upgraded safely
    legacy_path = tmp_path / "legacy_ok.db"
    legacy_url = f"sqlite:///{legacy_path}"
    _seed_adf094dd1fee_database(legacy_path)
    conn = sqlite3.connect(str(legacy_path))
    conn.execute("DROP TABLE alembic_version")
    conn.commit()
    conn.close()

    upgraded = prepare_and_upgrade_database(legacy_url, backup_dir=tmp_path / "backups")
    assert upgraded["action"] == "stamped_and_upgraded"
    assert upgraded["head"] == "c2e4a6b8d002"
    assert upgraded["backup_path"] is not None

    # 2. Drifted unversioned DB -> backed up and rejected cleanly before touching data
    drift_path = tmp_path / "legacy_drift.db"
    drift_url = f"sqlite:///{drift_path}"
    _seed_adf094dd1fee_database(drift_path)
    conn2 = sqlite3.connect(str(drift_path))
    conn2.execute("DROP TABLE alembic_version")
    conn2.execute("CREATE TABLE rogue_table (id TEXT PRIMARY KEY)")
    conn2.commit()
    conn2.close()

    with pytest.raises(SchemaMismatchError, match="does not match initial Alembic revision"):
        prepare_and_upgrade_database(drift_url, backup_dir=tmp_path / "backups")
