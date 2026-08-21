"""add_alerts_partial_unique

Adds the partial unique index ``(node_id, parameter) WHERE acknowledged_at
IS NULL`` on ``alerts``, backing the escalation-aware alert de-dupe (M-4).

Before this migration an alert for a node+parameter was suppressed by *any*
outstanding unacknowledged alert (an unacknowledged warning silently blocked
a later critical) and there was no constraint preventing double-inserts when
two Celery workers ran ``check_thresholds`` concurrently.

The index guarantees at most one unacknowledged alert per (node_id, parameter);
``tasks.check_thresholds`` upserts against it (INSERT … ON CONFLICT) so the DB
arbitrates races, and only a higher-severity breach replaces the existing row.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # NOTE: if the existing ``alerts`` table already holds multiple
    # unacknowledged rows for the same (node_id, parameter), this index
    # creation will fail — de-duplicate those rows first (e.g. keep the most
    # severe) so the index can be built.
    op.create_index(
        "uq_alerts_unacked_node",
        "alerts",
        ["node_id", "parameter"],
        unique=True,
        postgresql_where=sa.text("acknowledged_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_alerts_unacked_node",
        table_name="alerts",
        postgresql_where=sa.text("acknowledged_at IS NULL"),
    )
