"""add_server_defaults

Add DB-side ``server_default`` values to the 8 NOT NULL columns that only
had Python-side ORM defaults (M-17). Raw / bulk SQL inserts (e.g. ``COPY``,
``INSERT INTO ... SELECT``) previously hit NOT NULL violations, and
``alembic --autogenerate`` could not see the drift.

Columns untouched (already have server_default): ``*.created_at``,
``*.updated_at``, ``registered_at``, ``triggered_at``, ``is_anomaly``.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-05

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (table, column, server_default) — matched to the ORM defaults in the models.
_SERVER_DEFAULTS: list[tuple[str, str, str]] = [
    # NOTE: must be a quoted literal ('user'), not bare `user` — in PostgreSQL
    # a bare `user` is the current_user keyword and would mis-default raw inserts.
    ("users", "role", "'user'"),
    ("users", "notification_prefs", "'{}'::jsonb"),
    ("users", "is_active", "true"),
    ("refresh_tokens", "revoked", "false"),
    ("nodes", "reading_interval", "30"),
    ("nodes", "is_active", "true"),
    ("hourly_agg", "anomaly_count", "0"),
    ("hourly_agg", "reading_count", "0"),
]


def upgrade() -> None:
    for table, column, default in _SERVER_DEFAULTS:
        op.alter_column(table, column, server_default=sa.text(default))


def downgrade() -> None:
    # Drop the DB-side defaults; the ORM-level Python defaults are unaffected.
    for table, column, _default in _SERVER_DEFAULTS:
        op.alter_column(table, column, server_default=None)
