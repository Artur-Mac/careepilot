"""Phase 2: Browser CDP discovery runs, dismissals, job eligibility/summary cache, and legacy fixture cleanup.

Revision ID: c2e4a6b8d002
Revises: b1d2e3f4a501
Create Date: 2026-11-12 23:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c2e4a6b8d002"
down_revision: str | Sequence[str] | None = "b1d2e3f4a501"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    is_sqlite = conn.dialect.name == "sqlite"

    if is_sqlite:
        with op.get_context().autocommit_block():
            op.execute(sa.text("PRAGMA foreign_keys=OFF"))

    # 1. Purge remaining legacy mocked/fictional records before removing is_mocked / is_fictional_test.
    # Keep jobs only if referenced by a real Application or backed by non-mocked source records.
    conn.execute(sa.text("DELETE FROM job_source_records WHERE is_mocked = 1"))
    conn.execute(
        sa.text(
            """
            DELETE FROM job_postings
            WHERE NOT EXISTS (
                SELECT 1 FROM job_source_records js WHERE js.job_id = job_postings.id
            )
            AND NOT EXISTS (
                SELECT 1 FROM applications a WHERE a.job_id = job_postings.id
            )
            AND (
                source IN ('fictional_ats', 'manual_alerts', 'greenhouse', 'ashby', 'lever', 'smartrecruiters', 'arbeitnow', 'himalayas', 'remotive', 'brave_search', 'tavily_search', 'adzuna')
                OR employer_id IN (SELECT id FROM employers WHERE is_fictional_test = 1)
            )
            """
        )
    )
    conn.execute(
        sa.text(
            """
            DELETE FROM employers
            WHERE is_fictional_test = 1
            AND NOT EXISTS (SELECT 1 FROM job_postings jp WHERE jp.employer_id = employers.id)
            """
        )
    )
    conn.execute(
        sa.text(
            """
            DELETE FROM source_connectors
            WHERE name IN (
                'greenhouse', 'ashby', 'lever', 'himalayas', 'remotive',
                'arbeitnow', 'manual_alerts', 'smartrecruiters',
                'brave_search', 'tavily_search', 'adzuna', 'fictional_ats'
            )
            """
        )
    )

    # 2. Drop retired tables (scheduler_runs and job_requirements)
    op.drop_table("scheduler_runs")
    op.drop_table("job_requirements")

    # 3. Alter employers, job_postings, and job_source_records cleanly via batch mode
    with op.batch_alter_table("employers", schema=None) as batch_op:
        batch_op.drop_column("board_token")
        batch_op.drop_column("is_fictional_test")

    with op.batch_alter_table("job_postings", schema=None) as batch_op:
        batch_op.drop_column("apply_mode")
        batch_op.add_column(sa.Column("summary_text", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "eligibility_json",
                sa.JSON(),
                nullable=False,
                server_default=sa.text(
                    '\'{"remote_scope": "unspecified", "brazil_eligible": "unspecified", "reason": null}\''
                ),
            )
        )

    with op.batch_alter_table("job_source_records", schema=None) as batch_op:
        batch_op.drop_column("is_mocked")
        batch_op.add_column(
            sa.Column(
                "discovery_channel",
                sa.String(length=32),
                nullable=False,
                server_default="browser",
            )
        )

    # 4. Create job_dismissals and discovery_runs tables
    op.create_table(
        "job_dismissals",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["job_postings.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "job_id", name="uq_job_dismissal_user_job"),
    )
    op.create_index("ix_job_dismissal_user_created", "job_dismissals", ["user_id", "created_at"])

    op.create_table(
        "discovery_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("query_text", sa.String(length=300), nullable=False),
        sa.Column("queries_json", sa.JSON(), nullable=False),
        sa.Column("sources_json", sa.JSON(), nullable=False),
        sa.Column("source_errors_json", sa.JSON(), nullable=False),
        sa.Column("total_raw", sa.Integer(), nullable=False),
        sa.Column("inserted_count", sa.Integer(), nullable=False),
        sa.Column("deduped_count", sa.Integer(), nullable=False),
        sa.Column("rejected_restricted_count", sa.Integer(), nullable=False),
        sa.Column("matched_job_ids_json", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_discovery_run_user_started", "discovery_runs", ["user_id", "started_at"])
    op.create_index("ix_discovery_run_status", "discovery_runs", ["status"])

    if is_sqlite:
        fk_violations = conn.execute(sa.text("PRAGMA foreign_key_check")).fetchall()
        if fk_violations:
            raise RuntimeError(f"SQLite foreign_key_check failed after Phase 2 migration: {fk_violations}")
        with op.get_context().autocommit_block():
            op.execute(sa.text("PRAGMA foreign_keys=ON"))


def downgrade() -> None:
    raise RuntimeError(
        "Destructive downgrade from Phase 2 migration c2e4a6b8d002 is unsupported. "
        "Stop CareerPilot and restore the SQLite backup created before upgrading "
        "(see <data_dir>/backups/)."
    )
