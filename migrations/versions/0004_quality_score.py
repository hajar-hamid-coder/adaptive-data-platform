"""Quality score per run: append-only quality_score (P4-03).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "quality_score",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_name", sa.String(255), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=True),
        sa.Column("scope", sa.String(8), nullable=False),
        sa.Column("column_name", sa.String(255), nullable=False),
        sa.Column("completeness", sa.Float(), nullable=True),
        sa.Column("validity", sa.Float(), nullable=True),
        sa.Column("uniqueness", sa.Float(), nullable=True),
        sa.Column("freshness", sa.Float(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("rows_total", sa.Integer(), nullable=False),
        sa.Column("rows_failed", sa.Integer(), nullable=False),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "source_name", "run_id", "scope", "column_name", name="uq_quality_score_run"
        ),
    )


def downgrade() -> None:
    op.drop_table("quality_score")
