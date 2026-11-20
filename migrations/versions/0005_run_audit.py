"""Run and step audit journal: run_audit, step_audit (P6-01).

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STEPS = "'bronze', 'schema_check', 'silver', 'promote'"
_STATUSES = "'success', 'failed', 'blocked', 'quarantined'"


def upgrade() -> None:
    op.create_table(
        "run_audit",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("source_name", sa.String(255), nullable=False),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=True),
        sa.Column("rows_in", sa.Integer(), nullable=True),
        sa.Column("rows_out", sa.Integer(), nullable=True),
        sa.Column("rows_quarantined", sa.Integer(), nullable=True),
        sa.Column("quality_score", sa.Float(), nullable=True),
        sa.Column("batch_id", sa.String(128), nullable=True),
        sa.Column("error_type", sa.String(64), nullable=True),
        sa.UniqueConstraint("run_id", "source_name", name="uq_run_audit_run"),
        sa.CheckConstraint(f"status IN ({_STATUSES}, 'running')", name="ck_run_audit_status"),
    )
    op.create_index("ix_run_audit_source_started", "run_audit", ["source_name", "started_at"])
    op.create_table(
        "step_audit",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("source_name", sa.String(255), nullable=False),
        sa.Column("step", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("rows_in", sa.Integer(), nullable=True),
        sa.Column("rows_out", sa.Integer(), nullable=True),
        sa.Column("rows_quarantined", sa.Integer(), nullable=True),
        sa.Column("quality_score", sa.Float(), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=True),
        sa.Column("schema_changes", sa.Integer(), nullable=True),
        sa.Column("impact", sa.String(16), nullable=True),
        sa.Column("batch_id", sa.String(128), nullable=True),
        sa.Column("reasons", sa.Text(), nullable=False, server_default=""),
        sa.Column("error_type", sa.String(64), nullable=True),
        sa.UniqueConstraint("run_id", "source_name", "step", name="uq_step_audit_step"),
        sa.ForeignKeyConstraint(
            ["run_id", "source_name"], ["run_audit.run_id", "run_audit.source_name"],
            name="fk_step_audit_run",
        ),
        sa.CheckConstraint(f"status IN ({_STATUSES})", name="ck_step_audit_status"),
        sa.CheckConstraint(f"step IN ({_STEPS})", name="ck_step_audit_step"),
    )
    op.create_index("ix_step_audit_step", "step_audit", ["step", "source_name"])


def downgrade() -> None:
    op.drop_table("step_audit")
    op.drop_table("run_audit")
