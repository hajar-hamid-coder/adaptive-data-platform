"""Schema registry: acceptance marker `schema_version.accepted_at` (P2-06 gate fix).

Backfill: versions registered before this revision are accepted unless their change log holds
a non auto-applied action (`queue_review` or `block`), i.e. what the gate let through so far.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# detail_json is written by json.dumps(sort_keys=True): `"action": "<value>"`
_BACKFILL = """
UPDATE schema_version SET accepted_at = created_at
WHERE NOT EXISTS (
    SELECT 1 FROM schema_change c
    WHERE c.source_name = schema_version.source_name
      AND c.to_version = schema_version.version
      AND (c.detail_json LIKE '%"action": "queue_review"%'
           OR c.detail_json LIKE '%"action": "block"%')
)
"""


def upgrade() -> None:
    with op.batch_alter_table("schema_version") as batch:
        batch.add_column(sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(_BACKFILL)


def downgrade() -> None:
    with op.batch_alter_table("schema_version") as batch:
        batch.drop_column("accepted_at")
