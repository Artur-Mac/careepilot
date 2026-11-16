"""Phase 3: Migrate non-sensitive legacy standard_answers into draft Arsenal entries.

Revision ID: e4f6a8b0c204
Revises: d3f5a7c9e103
Create Date: 2026-11-13 22:25:00.000000
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.orm import Session

from alembic import op
from backend.app.services.applications.arsenal import migrate_legacy_standard_answers_to_arsenal

revision: str = "e4f6a8b0c204"
down_revision: str | Sequence[str] | None = "d3f5a7c9e103"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    conn = op.get_bind()
    is_sqlite = conn.dialect.name == "sqlite"

    user_ids = [
        str(row[0])
        for row in conn.execute(
            sa.text("SELECT user_id FROM master_profiles ORDER BY user_id ASC")
        ).fetchall()
        if row[0]
    ]
    if user_ids:
        orm_session = Session(bind=conn)
        migrated_total = 0
        for uid in user_ids:
            migrated_total += migrate_legacy_standard_answers_to_arsenal(orm_session, uid)
        orm_session.flush()
        orm_session.expunge_all()
        logger.info(
            "Phase 3 legacy standard_answers migration complete: %s draft(s) created",
            migrated_total,
        )

    if is_sqlite:
        fk_violations = conn.execute(sa.text("PRAGMA foreign_key_check")).fetchall()
        if fk_violations:
            raise RuntimeError(
                f"SQLite foreign_key_check failed after Phase 3 Arsenal migration: {fk_violations}"
            )
        with op.get_context().autocommit_block():
            op.execute(sa.text("PRAGMA foreign_keys=ON"))


def downgrade() -> None:
    raise RuntimeError(
        "Destructive downgrade from migration e4f6a8b0c204 is unsupported. "
        "Stop CareerPilot and restore the SQLite backup created before upgrading "
        "(see <data_dir>/backups/)."
    )
