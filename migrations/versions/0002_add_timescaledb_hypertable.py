"""
add_timescaledb_hypertable

Installs the TimescaleDB extension and converts ``sensor_readings``
(and eventually ``hourly_agg``) to hypertables for efficient
time-series queries.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-30 18:17:22.880079

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. Install TimescaleDB (safe to re-run) ──────────────────────────────
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE")

    # ── 2. Convert sensor_readings to a hypertable ───────────────────────────
    # The composite PK (time, node_id) already includes the partition column.
    op.execute(
        "SELECT create_hypertable("
        "  'sensor_readings', 'time',"
        "  if_not_exists => TRUE,"
        "  migrate_data => TRUE"
        ")"
    )


def downgrade() -> None:
    # TimescaleDB does not support converting a hypertable back to a plain
    # table directly.  Since we are in early development (no critical data),
    # recreate sensor_readings as a plain table.
    #
    # ``LIKE ... INCLUDING ALL`` copies the table's constraints and indexes
    # (with their names), so they must NOT be re-created below.
    op.execute("CREATE TABLE sensor_readings_plain (LIKE sensor_readings INCLUDING ALL)")
    op.execute("INSERT INTO sensor_readings_plain SELECT * FROM sensor_readings")
    op.execute("DROP TABLE sensor_readings")
    op.execute("ALTER TABLE sensor_readings_plain RENAME TO sensor_readings")

    # Note: we do NOT drop the timescaledb extension here — other databases
    # in the cluster may depend on it.
