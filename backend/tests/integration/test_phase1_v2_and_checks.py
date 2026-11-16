"""Integration tests for Phase 1 SQLite constraints, CAS import conflicts, repair-preview, export, and wipe."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import inspect, select

from alembic import command
from backend.app.core.config import Settings
from backend.app.core.time import utc_rfc3339
from backend.app.db.migrations import (
    build_alembic_config,
    get_script_head,
    prepare_and_upgrade_database,
)
from backend.app.db.session import build_engine, get_session_factory
from backend.app.models.tables import (
    Application,
    ArsenalRespostaRow,
    JobDiscoveryFeedbackRow,
    JobPosting,
    PendingManualActionRow,
    PortalSessionStatusRow,
    ProfileChangeProposalRow,
    SearchTuningRuleRow,
    UserSettings,
)
from backend.app.schemas.profile import MasterProfileDocumentV2
from backend.app.services.profile.service import ensure_user_settings


def test_sqlite_check_constraints_partial_uniques_and_foreign_keys_at_head(tmp_path: Path) -> None:
    db_path = tmp_path / "constraints.db"
    db_url = f"sqlite:///{db_path}"
    res = prepare_and_upgrade_database(db_url)
    assert res["head"] == get_script_head()

    engine = build_engine(db_url)
    try:
        tables = set(inspect(engine).get_table_names())
        expected_tables = {
            "portal_session_status",
            "pending_manual_actions",
            "arsenal_respostas",
            "profile_change_proposals",
            "job_discovery_feedback",
            "search_tuning_rules",
        }
        assert expected_tables.issubset(tables)

        settings_cols = {c["name"] for c in inspect(engine).get_columns("user_settings")}
        job_cols = {c["name"] for c in inspect(engine).get_columns("job_postings")}
        run_cols = {c["name"] for c in inspect(engine).get_columns("discovery_runs")}
        rv_cols = {c["name"] for c in inspect(engine).get_columns("resume_versions")}
        assert "salary_policy_json" in settings_cols
        assert "requirements_json" in job_cols
        assert "plan_json" in run_cols
        assert {"markdown_path", "artifact_key"}.issubset(rv_cols)
    finally:
        engine.dispose()

    now = utc_rfc3339()
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")

        # Seed parent job + application + resume
        conn.execute(
            "INSERT INTO job_postings(id, canonical_url, dedup_signature, source, source_job_id, "
            "company, title, workplace_type, description, description_hash, job_url, source_attribution, "
            "eligibility_json, requirements_json, discovered_at, is_expired, injection_signals) VALUES "
            "('job-1', 'https://ex.org/1', 'sig-1', 'remotive', 'j1', 'Visagio', 'Data Eng', "
            "'remote', 'Python SQL', ?, 'https://ex.org/1', 'Remotive', '{}', '[]', ?, 0, '[]')",
            ("h" * 64, now),
        )
        conn.execute(
            "INSERT INTO applications(id, job_id, user_id, status, answers_json, created_at, updated_at) "
            "VALUES ('app-1', 'job-1', 'local-applicant', 'ready', '{}', ?, ?)",
            (now, now),
        )

        # 1. portal_session_status CHECK: authenticated requires expires_at > checked_at + browser_generation
        try:
            conn.execute(
                "INSERT INTO portal_session_status(user_id, portal_key, scope_key, status, checked_at, expires_at, revision) "
                "VALUES ('local-applicant', 'linkedin', 'default', 'authenticated', ?, ?, 1)",
                (now, now),
            )
            raise AssertionError("Expected CHECK violation on portal_session_status")
        except sqlite3.IntegrityError:
            pass

        # 2. pending_manual_actions CHECK: approved requires approval_sealed + approved_at;
        # pending requires approval_sealed IS NULL
        try:
            conn.execute(
                "INSERT INTO pending_manual_actions(id, user_id, application_id, step_key, field_key, "
                "field_fingerprint, kind, status, field_json, approval_sealed, revision, created_at, updated_at) "
                "VALUES ('pa-bad', 'local-applicant', 'app-1', 's1', 'f1', ?, 'salary', 'pending', '{}', X'0102', 1, ?, ?)",
                ("f" * 64, now, now),
            )
            raise AssertionError("Expected CHECK violation on pending with approval_sealed")
        except sqlite3.IntegrityError:
            pass

        # Partial unique index allows multiple 'stale' rows while forbidding duplicate active row
        conn.execute(
            "INSERT INTO pending_manual_actions(id, user_id, application_id, step_key, field_key, "
            "field_fingerprint, kind, status, field_json, revision, created_at, updated_at) "
            "VALUES ('pa-stale-1', 'local-applicant', 'app-1', 's1', 'f1', ?, 'salary', 'stale', '{}', 1, ?, ?)",
            ("f" * 64, now, now),
        )
        conn.execute(
            "INSERT INTO pending_manual_actions(id, user_id, application_id, step_key, field_key, "
            "field_fingerprint, kind, status, field_json, revision, created_at, updated_at) "
            "VALUES ('pa-stale-2', 'local-applicant', 'app-1', 's1', 'f1', ?, 'salary', 'stale', '{}', 1, ?, ?)",
            ("f" * 64, now, now),
        )
        conn.execute(
            "INSERT INTO pending_manual_actions(id, user_id, application_id, step_key, field_key, "
            "field_fingerprint, kind, status, field_json, revision, created_at, updated_at) "
            "VALUES ('pa-active-1', 'local-applicant', 'app-1', 's1', 'f1', ?, 'salary', 'pending', '{}', 1, ?, ?)",
            ("f" * 64, now, now),
        )
        try:
            conn.execute(
                "INSERT INTO pending_manual_actions(id, user_id, application_id, step_key, field_key, "
                "field_fingerprint, kind, status, field_json, revision, created_at, updated_at) "
                "VALUES ('pa-active-2', 'local-applicant', 'app-1', 's1', 'f1', ?, 'salary', 'pending', '{}', 1, ?, ?)",
                ("f" * 64, now, now),
            )
            raise AssertionError("Expected partial unique violation on active pending_manual_actions")
        except sqlite3.IntegrityError:
            pass

        # 3. arsenal_respostas partial unique index: multiple retired OK, two approved for same id blocked
        conn.execute(
            "INSERT INTO arsenal_respostas(user_id, id, revision, status, language, role_archetype, category, payload_json, approved_at, created_at, updated_at) "
            "VALUES ('local-applicant', 'ars-1', 1, 'approved', 'pt-BR', 'all', 'custom', '{}', ?, ?, ?)",
            (now, now, now),
        )
        try:
            conn.execute(
                "INSERT INTO arsenal_respostas(user_id, id, revision, status, language, role_archetype, category, payload_json, approved_at, created_at, updated_at) "
                "VALUES ('local-applicant', 'ars-1', 2, 'approved', 'pt-BR', 'all', 'custom', '{}', ?, ?, ?)",
                (now, now, now),
            )
            raise AssertionError("Expected partial unique violation on arsenal_respostas approved")
        except sqlite3.IntegrityError:
            pass

        # 4. profile_change_proposals CHECK: expires_at > created_at and sealed state exclusivity
        try:
            conn.execute(
                "INSERT INTO profile_change_proposals(id, user_id, base_revision, status, proposal_sealed, created_at, expires_at) "
                "VALUES ('prop-bad', 'local-applicant', 1, 'draft', X'01', '2026-11-10T10:00:00.000000Z', '2026-11-10T09:00:00.000000Z')"
            )
            raise AssertionError("Expected CHECK violation for expires_at <= created_at")
        except sqlite3.IntegrityError:
            pass

        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_repair_preview_preserves_manual_edits_regroups_fragments_and_handles_conflicts(
    client: TestClient,
    auth_headers: dict[str, str],
) -> None:
    raw_resume = (
        "Artur Albuquerque Machado\n"
        "arturmac10@gmail.com | https://www.linkedin.com/in/artur-albuquerque-machado/\n\n"
        "Experiência Profissional\n"
        "Visagio - Engenheiro de Dados | Out 2025 - Atual\n"
        "- Orquestracao Azure Data Factory e Databricks.\n"
        "- FinOps reduzindo 40% dos custos cloud.\n"
        "Grupo Iquine - Estagiário de Dados | Ago 2025 - Set 2025\n"
        "- Pipeline IDP com PaddleOCR e spaCy NER.\n\n"
        "Formação Acadêmica\n"
        "Universidade Federal de Pernambuco (UFPE) - Ciência da Computação | 2022 - 2026\n\n"
        "Habilidades\n"
        "Python, SQL, Azure, C++, .NET\n"
    ).encode()

    # Unknown document repair preview -> 404
    missing = client.post(
        "/api/profile/imports/imp-does-not-exist/repair-preview",
        json={"base_revision": 0},
        headers=auth_headers,
    )
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "import_not_found"

    # Import at expected revision 0
    imp = client.post(
        "/api/profile/import",
        files={"file": ("curriculo.txt", raw_resume, "text/plain")},
        data={"base_revision": "0"},
        headers=auth_headers,
    )
    assert imp.status_code == 200
    body = imp.json()
    assert body["revision"] == 1
    assert body["import_result"]["duplicate"] is False
    assert body["import_result"]["counts"]["experience"] == 2
    assert body["import_result"]["counts"]["education"] == 1
    doc_id = body["import_result"]["document_id"]

    # Importing again with stale base_revision=0 -> 409 conflict
    stale_imp = client.post(
        "/api/profile/import",
        files={"file": ("other.txt", b"Outro Curriculo\nPython, SQL", "text/plain")},
        data={"base_revision": "0"},
        headers=auth_headers,
    )
    assert stale_imp.status_code == 409
    assert stale_imp.json()["detail"]["code"] == "stale_revision"
    assert stale_imp.json()["detail"]["current"]["revision"] == 1

    # Simulate broken historical import state (fragments + 0 education + empty skill + user-edited entry + summary ref)
    broken_doc = body["document"]
    broken_doc["experience"] = [
        {
            "id": "frag-1",
            "company": None,
            "role": None,
            "period": None,
            "details": "Orquestracao Azure Data Factory e Databricks.",
            "bullets": [],
            "source": "upload:curriculo.txt",
            "evidence": [{"document_name": "curriculo.txt", "source_sha256": body["import_summary"]["sha256"]}],
        },
        {
            "id": "frag-2",
            "company": None,
            "role": None,
            "period": None,
            "details": "FinOps reduzindo 40% dos custos cloud.",
            "bullets": [],
            "source": "upload:curriculo.txt",
            "evidence": [{"document_name": "curriculo.txt", "source_sha256": body["import_summary"]["sha256"]}],
        },
        {
            "id": "exp-manual-kept",
            "company": "Empresa Manual",
            "role": "Consultor",
            "period": "2024",
            "details": "Experiencia escrita manualmente.",
            "bullets": [{"id": "eb-man-1", "text": "Projeto manual unico."}],
            "source": "manual",
            "evidence": [],
        },
    ]
    broken_doc["education"] = []
    broken_doc["skills"].append({"id": "sk-empty", "name": "---", "source": "upload:curriculo.txt"})
    broken_doc["summaries"] = [
        {
            "id": "sum-1",
            "language": "pt-BR",
            "text": "Engenheiro focado em Azure e FinOps.",
            "source_claim_ids": ["frag-1"],
        }
    ]
    put_broken = client.put(
        "/api/profile",
        json={"document": broken_doc, "revision": 1},
        headers=auth_headers,
    )
    assert put_broken.status_code == 200
    assert put_broken.json()["revision"] == 2

    # Stale repair-preview revision -> 409
    stale_rp = client.post(
        f"/api/profile/imports/{doc_id}/repair-preview",
        json={"base_revision": 1},
        headers=auth_headers,
    )
    assert stale_rp.status_code == 409

    # Valid repair-preview -> non-destructive diff fixing fragments, keeping manual, recovering edu, remapping claim IDs
    preview = client.post(
        f"/api/profile/imports/{doc_id}/repair-preview",
        json={"base_revision": 2},
        headers=auth_headers,
    )
    assert preview.status_code == 200
    pdata = preview.json()
    assert pdata["diff"]["removed_empty_skills"] == ["sk-empty"]
    assert "exp-manual-kept" in pdata["diff"]["preserved_user_edited_ids"]
    assert len(pdata["diff"]["recovered_education"]) == 1
    assert len(pdata["diff"]["grouped_replacements"]) == 1
    assert pdata["diff"]["id_mapping"].get("frag-1")

    proposed = MasterProfileDocumentV2.model_validate(pdata["proposed_document"])
    assert any(e.id == "exp-manual-kept" for e in proposed.experience)
    assert any(e.company == "Visagio" and len(e.bullets) == 2 for e in proposed.experience)
    assert len(proposed.education) == 1
    assert all(s.id != "sk-empty" for s in proposed.skills)
    assert proposed.summaries[0].source_claim_ids[0] != "frag-1"

    # Non-destructive check: GET /api/profile still at revision 2 until user explicitly applies via PUT
    still_cur = client.get("/api/profile", headers=auth_headers).json()
    assert still_cur["revision"] == 2
    assert len(still_cur["document"]["education"]) == 0


def test_export_and_delete_cover_governed_tables_without_leaking_sealed_blobs(
    client: TestClient,
    isolated_settings: Settings,
    auth_headers: dict[str, str],
) -> None:
    sf = get_session_factory(isolated_settings)
    now = utc_rfc3339()
    future = "2027-01-01T00:00:00.000000Z"

    with sf() as db:
        us = ensure_user_settings(db, user_id="local-applicant")
        us.salary_policy_json = [{"id": "b-1", "min_minor": 500_000}]

        db.add(
            JobPosting(
                id="job-exp-1",
                canonical_url="https://example.org/j/1",
                dedup_signature="sig-exp-1",
                source="remotive",
                source_job_id="exp-1",
                company="Visagio",
                title="Data Engineer",
                workplace_type="remote",
                description="Python SQL Azure",
                description_hash="b" * 64,
                job_url="https://example.org/j/1",
                source_attribution="Remotive",
            )
        )
        db.flush()
        db.add(Application(id="app-exp-1", job_id="job-exp-1", user_id="local-applicant", status="ready"))
        db.flush()
        db.add(
            PortalSessionStatusRow(
                portal_key="linkedin",
                scope_key="default",
                status="authenticated",
                browser_generation="gen-1",
                checked_at=now,
                expires_at=future,
                revision=1,
            )
        )
        db.add(
            PendingManualActionRow(
                id="pma-1",
                application_id="app-exp-1",
                step_key="step-1",
                field_key="sal-1",
                field_fingerprint="c" * 64,
                kind="salary",
                status="approved",
                field_json={"label": "Pretensao", "control": "text", "required": True},
                approval_sealed=b"TOP_SECRET_FERNET_BLOB",
                approved_at=now,
                revision=1,
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            ArsenalRespostaRow(
                id="ars-1",
                revision=1,
                status="approved",
                language="pt-BR",
                role_archetype="data_engineer",
                category="finops_cloud_impact",
                payload_json={"trigger_keywords": ["finops"], "length_variants": {"short": "FinOps Azure."}},
                approved_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        db.add(
            ProfileChangeProposalRow(
                id="prop-1",
                base_revision=0,
                status="draft",
                proposal_sealed=b"TOP_SECRET_PROPOSAL_BLOB",
                created_at=now,
                expires_at=future,
            )
        )
        db.add(
            JobDiscoveryFeedbackRow(
                id="fb-exp-1",
                job_id="job-exp-1",
                action="high_fit",
                operation="apply",
                dedup_key="dedup-fb-1",
                evidence_json={"archetype": "data_engineer"},
                created_at=now,
            )
        )
        db.add(SearchTuningRuleRow(revision=1, rules_json={"version": 1, "keyword_weights": {"python": 5}}, updated_at=now))
        db.commit()

    exp_resp = client.get("/api/export", headers=auth_headers)
    assert exp_resp.status_code == 200
    exp_text = exp_resp.text
    assert "TOP_SECRET_FERNET_BLOB" not in exp_text
    assert "TOP_SECRET_PROPOSAL_BLOB" not in exp_text
    exp_data = json.loads(exp_text)
    assert exp_data["salary_policy"][0]["id"] == "b-1"
    assert len(exp_data["arsenal_respostas"]) == 1
    assert len(exp_data["pending_manual_actions"]) == 1
    assert "approval_sealed" not in exp_data["pending_manual_actions"][0]
    assert len(exp_data["portal_session_status"]) == 1
    assert len(exp_data["profile_change_proposals"]) == 1
    assert "proposal_sealed" not in exp_data["profile_change_proposals"][0]
    assert len(exp_data["job_discovery_feedback"]) == 1
    assert exp_data["search_tuning_rules"]["keyword_weights"]["python"] == 5

    del_resp = client.delete("/api/data", headers=auth_headers)
    assert del_resp.status_code == 200
    with sf() as db:
        us_after = db.scalar(select(UserSettings).where(UserSettings.user_id == "local-applicant"))
        assert us_after is not None and us_after.salary_policy_json == []
        for model in (
            PendingManualActionRow,
            JobDiscoveryFeedbackRow,
            ArsenalRespostaRow,
            ProfileChangeProposalRow,
            PortalSessionStatusRow,
            SearchTuningRuleRow,
            Application,
            JobPosting,
        ):
            assert db.query(model).count() == 0


def test_b1d2_migration_path_also_upgrades_cleanly_to_head(tmp_path: Path) -> None:
    db_path = tmp_path / "b1d2.db"
    db_url = f"sqlite:///{db_path}"
    command.upgrade(build_alembic_config(db_url), "b1d2e3f4a501")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO master_profiles(user_id, document, revision, updated_at) VALUES (?, ?, 1, ?)",
            (
                "local-applicant",
                json.dumps({"contact": {"name": "Artur"}, "experience": [{"id": "e1", "company": "Visagio"}]}),
                utc_rfc3339(),
            ),
        )
        conn.commit()

    res = prepare_and_upgrade_database(db_url, backup_dir=tmp_path / "backups")
    assert res["action"] == "upgraded"
    assert res["head"] == get_script_head()
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT document FROM master_profiles WHERE user_id='local-applicant'").fetchone()
        assert row is not None
        doc = json.loads(row[0])
        assert doc["schema_version"] == 2
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
