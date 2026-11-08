"""Schema registry: schema_version and schema_change tables.

Revision ID: 0001
Revises:
Create Date: 2026-11-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "schema_version",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_name", sa.String(255), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("semver", sa.String(32), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("columns_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_name", "version", name="uq_schema_version_source_version"),
        sa.UniqueConstraint(
            "source_name", "fingerprint", name="uq_schema_version_source_fingerprint"
        ),
    )
    op.create_table(
        "schema_change",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_name", sa.String(255), nullable=False),
        sa.Column("from_version", sa.Integer(), nullable=True),
        sa.Column("to_version", sa.Integer(), nullable=False),
        sa.Column("change_type", sa.String(64), nullable=False),
        sa.Column("column_name", sa.String(255), nullable=True),
        sa.Column("detail_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_schema_change_source_name", "schema_change", ["source_name"])


def downgrade() -> None:
    op.drop_index("ix_schema_change_source_name", table_name="schema_change")
    op.drop_table("schema_change")
    op.drop_table("schema_version")
