"""Phase 1: Governed tables, profile v2 conversion, and discovery/resume/settings columns.

Revision ID: d3f5a7c9e103
Revises: c2e4a6b8d002
Create Date: 2026-11-13 12:15:00.000000
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from alembic import op
from backend.app.schemas.profile import upgrade_profile_document_v1_to_v2

revision: str = "d3f5a7c9e103"
down_revision: str | Sequence[str] | None = "c2e4a6b8d002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")


def _convert_profile_row(raw_document: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Decode and upgrade one raw master_profiles.document payload to schema_version=2."""
    parsed = raw_document
    if isinstance(raw_document, str):
        parsed = json.loads(raw_document)
    if not isinstance(parsed, dict):
        raise ValueError("MasterProfile document is not a JSON object.")
    return upgrade_profile_document_v1_to_v2(parsed)


def _backfill_master_profiles_v2(conn: sa.Connection) -> dict[str, int]:
    """Upgrade all existing master_profiles rows to v2 inside the migration transaction."""
    rows = conn.execute(
        sa.text("SELECT user_id, document, revision FROM master_profiles ORDER BY user_id ASC")
    ).fetchall()
    summary = {"scanned": len(rows), "upgraded": 0, "unchanged": 0, "malformed": 0}
    now_dt = datetime.now(UTC)

    for user_id, raw_doc, existing_rev in rows:
        try:
            upgraded_json, diag = _convert_profile_row(raw_doc)
        except Exception as exc:
            summary["malformed"] += 1
            logger.error(
                "Aborting MasterProfile v2 conversion: malformed rows=%d (user_id=%s): %s",
                summary["malformed"],
                user_id,
                type(exc).__name__,
            )
            raise RuntimeError(
                f"Falha na conversão de MasterProfile para v2 (linhas malformadas={summary['malformed']}). "
                "Nenhuma alteração foi aplicada."
            ) from exc

        if not diag["changed"]:
            summary["unchanged"] += 1
            continue

        old_rev = int(existing_rev) if existing_rev is not None else 1
        new_rev = old_rev + 1
        conn.execute(
            sa.text(
                "UPDATE master_profiles SET document = :doc, revision = :rev, updated_at = :ts "
                "WHERE user_id = :uid AND revision = :old_rev"
            ),
            {
                "doc": json.dumps(upgraded_json, ensure_ascii=False),
                "rev": new_rev,
                "ts": now_dt,
                "uid": str(user_id),
                "old_rev": old_rev,
            },
        )
        conn.execute(
            sa.text(
                "INSERT INTO audit_events (id, actor, event_type, entity_type, entity_id, details, created_at) "
                "VALUES (:id, :actor, :event_type, :entity_type, :entity_id, :details, :created_at)"
            ),
            {
                "id": str(uuid.uuid4()),
                "actor": "alembic",
                "event_type": "migration.master_profile_v2",
                "entity_type": "master_profile",
                "entity_id": str(user_id),
                "details": json.dumps(
                    {
                        "from_revision": old_rev,
                        "to_revision": new_rev,
                        "counts": diag["counts"],
                    },
                    ensure_ascii=False,
                ),
                "created_at": now_dt,
            },
        )
        summary["upgraded"] += 1

    logger.info(
        "MasterProfile v2 conversion summary: scanned=%d upgraded=%d unchanged=%d malformed=%d",
        summary["scanned"],
        summary["upgraded"],
        summary["unchanged"],
        summary["malformed"],
    )
    return summary


def upgrade() -> None:
    conn = op.get_bind()
    is_sqlite = conn.dialect.name == "sqlite"

    if is_sqlite:
        with op.get_context().autocommit_block():
            op.execute(sa.text("PRAGMA foreign_keys=OFF"))
        # SQLite's legacy driver does not begin a transaction for DDL.
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    # 1. portal_session_status
    op.create_table(
        "portal_session_status",
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("portal_key", sa.Text(), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=True),
        sa.Column("browser_generation", sa.Text(), nullable=True),
        sa.Column("checked_at", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.Text(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.PrimaryKeyConstraint("user_id", "portal_key", "scope_key"),
        sa.CheckConstraint(
            "status IN ('unknown','checking','authenticated','login_required','public_access','challenge','unavailable','disconnected')",
            name="ck_portal_session_status_value",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_portal_session_revision"),
        sa.CheckConstraint(
            "status NOT IN ('authenticated','public_access','login_required','challenge') "
            "OR (browser_generation IS NOT NULL AND checked_at IS NOT NULL AND expires_at > checked_at AND expires_at IS NOT NULL)",
            name="ck_portal_session_fresh_evidence",
        ),
    )

    # 2. Composite unique index on applications for FK binding
    op.create_index("uq_applications_id_user_v2", "applications", ["id", "user_id"], unique=True)

    # 3. pending_manual_actions
    op.create_table(
        "pending_manual_actions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("application_id", sa.Text(), nullable=False),
        sa.Column("step_key", sa.Text(), nullable=False),
        sa.Column("field_key", sa.Text(), nullable=False),
        sa.Column("field_fingerprint", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("field_json", sa.Text(), nullable=False),
        sa.Column("suggestion_json", sa.Text(), nullable=True),
        sa.Column("approval_sealed", sa.LargeBinary(), nullable=True),
        sa.Column("approved_at", sa.Text(), nullable=True),
        sa.Column("applied_at", sa.Text(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["application_id", "user_id"],
            ["applications.id", "applications.user_id"],
            ondelete="CASCADE",
            name="fk_pending_action_app_user",
        ),
        sa.CheckConstraint("length(field_fingerprint) = 64", name="ck_pending_fingerprint_len"),
        sa.CheckConstraint(
            "kind IN ('salary','sensitive','ambiguous','low_confidence','invalid','manual_step')",
            name="ck_pending_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending','approved','applied','skipped','stale')",
            name="ck_pending_status",
        ),
        sa.CheckConstraint(
            "json_valid(field_json) AND json_type(field_json) = 'object'",
            name="ck_pending_field_json",
        ),
        sa.CheckConstraint(
            "suggestion_json IS NULL OR (json_valid(suggestion_json) AND json_type(suggestion_json) = 'object')",
            name="ck_pending_suggestion_json",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_pending_revision"),
        sa.CheckConstraint(
            "status <> 'approved' OR (approval_sealed IS NOT NULL AND approved_at IS NOT NULL)",
            name="ck_pending_approved_sealed",
        ),
        sa.CheckConstraint(
            "status <> 'applied' OR (approved_at IS NOT NULL AND applied_at IS NOT NULL)",
            name="ck_pending_applied_timestamps",
        ),
        sa.CheckConstraint(
            "applied_at IS NULL OR status IN ('applied','stale')",
            name="ck_pending_applied_at_state",
        ),
        sa.CheckConstraint(
            "status NOT IN ('pending','skipped','stale','applied') OR approval_sealed IS NULL",
            name="ck_pending_unapproved_no_secret",
        ),
    )
    op.create_index(
        "uq_pending_action_active_field_v2",
        "pending_manual_actions",
        ["user_id", "application_id", "step_key", "field_key"],
        unique=True,
        sqlite_where=sa.text("status IN ('pending','approved','applied','skipped')"),
    )
    op.create_index(
        "ix_pending_action_inbox_v2",
        "pending_manual_actions",
        ["user_id", "status", "updated_at", "id"],
    )

    # 4. arsenal_respostas
    op.create_table(
        "arsenal_respostas",
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("language", sa.Text(), nullable=False),
        sa.Column("role_archetype", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("approved_at", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "id", "revision"),
        sa.CheckConstraint("revision >= 1", name="ck_arsenal_revision"),
        sa.CheckConstraint("status IN ('draft','approved','retired')", name="ck_arsenal_status"),
        sa.CheckConstraint("language IN ('pt-BR','en')", name="ck_arsenal_language"),
        sa.CheckConstraint(
            "role_archetype IN ('all','data_engineer','backend_python','ml_engineer','intern_junior')",
            name="ck_arsenal_archetype",
        ),
        sa.CheckConstraint(
            "category IN ('professional_summary','why_company','technical_challenge','finops_cloud_impact','availability','work_authorization','english_proficiency','salary_note','custom')",
            name="ck_arsenal_category",
        ),
        sa.CheckConstraint(
            "json_valid(payload_json) AND json_type(payload_json) = 'object'",
            name="ck_arsenal_payload_json",
        ),
        sa.CheckConstraint(
            "status <> 'approved' OR approved_at IS NOT NULL",
            name="ck_arsenal_approved_at",
        ),
    )
    op.create_index(
        "uq_arsenal_active_revision_v2",
        "arsenal_respostas",
        ["user_id", "id"],
        unique=True,
        sqlite_where=sa.text("status = 'approved'"),
    )
    op.create_index(
        "ix_arsenal_match_v2",
        "arsenal_respostas",
        ["user_id", "status", "language", "role_archetype", "category"],
    )

    # 5. profile_change_proposals
    op.create_table(
        "profile_change_proposals",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("base_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("proposal_sealed", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("resolved_at", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("base_revision >= 0", name="ck_proposal_base_revision"),
        sa.CheckConstraint(
            "status IN ('draft','applied','rejected','stale')",
            name="ck_proposal_status",
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_proposal_expires_gt_created"),
        sa.CheckConstraint(
            "(status = 'draft' AND proposal_sealed IS NOT NULL AND resolved_at IS NULL) "
            "OR (status <> 'draft' AND proposal_sealed IS NULL AND resolved_at IS NOT NULL)",
            name="ck_proposal_resolution_state",
        ),
    )
    op.create_index(
        "ix_profile_proposals_user_v2",
        "profile_change_proposals",
        ["user_id", "status", "expires_at"],
    )

    # 6. job_discovery_feedback
    op.create_table(
        "job_discovery_feedback",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("job_id", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("dedup_key", sa.Text(), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["job_id"], ["job_postings.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "id", name="uq_feedback_user_id"),
        sa.UniqueConstraint("user_id", "dedup_key", name="uq_feedback_user_dedup"),
        sa.CheckConstraint(
            "action IN ('high_fit','too_senior','wrong_tech_stack','onsite_outside_recife','blacklist_company')",
            name="ck_feedback_action",
        ),
        sa.CheckConstraint("operation IN ('apply','undo')", name="ck_feedback_operation"),
        sa.CheckConstraint(
            "json_valid(evidence_json) AND json_type(evidence_json) = 'object'",
            name="ck_feedback_evidence_json",
        ),
    )
    op.create_index(
        "ix_feedback_projection_v2",
        "job_discovery_feedback",
        ["user_id", "job_id", "action", "created_at", "id"],
    )

    # 7. search_tuning_rules
    op.create_table(
        "search_tuning_rules",
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("rules_json", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("user_id"),
        sa.CheckConstraint("revision >= 1", name="ck_tuning_revision"),
        sa.CheckConstraint(
            "json_valid(rules_json) AND json_type(rules_json) = 'object'",
            name="ck_tuning_rules_json",
        ),
    )

    # 8. Alter existing tables via SQLite-safe batch_alter_table
    with op.batch_alter_table("user_settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("salary_policy_json", sa.Text(), nullable=False, server_default=sa.text("'[]'"))
        )
        batch_op.create_check_constraint(
            "ck_user_settings_salary_policy_json",
            "json_valid(salary_policy_json) AND json_type(salary_policy_json) = 'array'",
        )

    with op.batch_alter_table("job_postings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("requirements_json", sa.Text(), nullable=False, server_default=sa.text("'[]'"))
        )
        batch_op.create_check_constraint(
            "ck_job_postings_requirements_json",
            "json_valid(requirements_json) AND json_type(requirements_json) = 'array'",
        )

    with op.batch_alter_table("discovery_runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("plan_json", sa.Text(), nullable=True))
        batch_op.create_check_constraint(
            "ck_discovery_runs_plan_json",
            "plan_json IS NULL OR (json_valid(plan_json) AND json_type(plan_json) = 'object')",
        )

    with op.batch_alter_table("resume_versions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("markdown_path", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("artifact_key", sa.Text(), nullable=True))
        batch_op.create_check_constraint(
            "ck_resume_versions_artifact_key",
            "artifact_key IS NULL OR length(artifact_key) = 64",
        )

    op.create_index(
        "uq_resume_artifact_v2",
        "resume_versions",
        ["user_id", "job_id", "artifact_key"],
        unique=True,
        sqlite_where=sa.text("artifact_key IS NOT NULL"),
    )

    # 9. Transactionally backfill existing MasterProfile documents from v1 -> v2
    _backfill_master_profiles_v2(conn)

    if is_sqlite:
        fk_violations = conn.execute(sa.text("PRAGMA foreign_key_check")).fetchall()
        if fk_violations:
            raise RuntimeError(
                f"SQLite foreign_key_check failed after Phase 1 v2 migration: {fk_violations}"
            )
        with op.get_context().autocommit_block():
            op.execute(sa.text("PRAGMA foreign_keys=ON"))


def downgrade() -> None:
    raise RuntimeError(
        "Destructive downgrade from migration d3f5a7c9e103 is unsupported. "
        "Stop CareerPilot and restore the SQLite backup created before upgrading "
        "(see <data_dir>/backups/)."
    )
