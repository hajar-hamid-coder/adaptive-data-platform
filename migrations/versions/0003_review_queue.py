"""Mapping human review: review_queue and append-only mapping_decision_log (P3-04).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "review_queue",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_name", sa.String(255), nullable=False),
        sa.Column("source_col", sa.String(255), nullable=False),
        sa.Column("target", sa.String(255), nullable=False),
        sa.Column("final_target", sa.String(255), nullable=True),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("method", sa.String(32), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("rationale", sa.String(200), nullable=False),
        sa.Column("reason", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(128), nullable=True),
        sa.Column("decision_note", sa.String(500), nullable=True),
        sa.UniqueConstraint(
            "source_name", "source_col", "target", "prompt_version",
            name="uq_review_queue_proposal",
        ),
    )  # fmt: skip
    op.create_index("ix_review_queue_status", "review_queue", ["status"])
    op.create_table(
        "mapping_decision_log",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("queue_id", sa.Integer(), nullable=True),
        sa.Column("source_name", sa.String(255), nullable=False),
        sa.Column("source_col", sa.String(255), nullable=False),
        sa.Column("target", sa.String(255), nullable=True),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("method", sa.String(32), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(128), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.String(500), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("mapping_decision_log")
    op.drop_index("ix_review_queue_status", table_name="review_queue")
    op.drop_table("review_queue")
