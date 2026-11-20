"""Auto-replay state: replay_attempt, replay_baseline (P6-03).

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUSES = ("planned", "running", "succeeded", "failed", "skipped", "exhausted", "reset")


def upgrade() -> None:
    op.create_table(
        "replay_attempt",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_name", sa.String(255), nullable=False),
        sa.Column("bronze_batch", sa.String(512), nullable=False),
        sa.Column("fix_id", sa.String(32), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actor", sa.String(128), nullable=True),
        sa.Column("note", sa.String(500), nullable=True),
        sa.UniqueConstraint(
            "source_name", "bronze_batch", "fix_id", "attempt_no", name="uq_replay_attempt_key"
        ),  # fmt: skip
        sa.CheckConstraint(
            f"status IN ({', '.join(repr(s) for s in _STATUSES)})", name="ck_replay_attempt_status"
        ),  # fmt: skip
    )
    op.create_index("ix_replay_attempt_batch", "replay_attempt", ["source_name", "bronze_batch"])
    op.create_table(
        "replay_baseline",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_name", sa.String(255), nullable=False),
        sa.Column("bronze_batch", sa.String(512), nullable=False),
        sa.Column("block_run_id", sa.String(64), nullable=False),
        sa.Column("config_fp", sa.String(64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "source_name", "bronze_batch", "block_run_id", name="uq_replay_baseline"
        ),  # fmt: skip
    )


def downgrade() -> None:
    op.drop_table("replay_baseline")
    op.drop_table("replay_attempt")
