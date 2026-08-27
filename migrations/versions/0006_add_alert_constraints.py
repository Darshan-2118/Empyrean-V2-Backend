"""add_alert_constraints

Three small schema fixes from the audit:

* M64 — the hot unacked-listing query
  (``WHERE acknowledged_at IS NULL ORDER BY triggered_at DESC``) had no
  supporting index; add a partial ``(triggered_at DESC)`` index.
* L25 — the alert-message cap (10 000 chars) was enforced only in the Celery
  task; add a CHECK constraint so the DB bounds storage too.
* M63 — ``refresh_tokens.token_hash`` is always a 64-char SHA-256 hex digest;
  shrink the over-allocated ``VARCHAR(255)`` (and its index) to ``VARCHAR(64)``.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-27

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # M64: partial index for the unacked-alert listing.
    op.create_index(
        "idx_alerts_unacked_triggered",
        "alerts",
        [sa.text("triggered_at DESC")],
        postgresql_where=sa.text("acknowledged_at IS NULL"),
    )
    # L25: bound alert message length at the DB level (matches
    # models.alert._MAX_ALERT_MESSAGE_LENGTH and the task-side cap). The name
    # follows the models' MetaData naming convention
    # ("%(table_name)s_%(constraint_name)s_check") so create_all and this
    # migration produce the identical constraint.
    op.create_check_constraint(
        "ck_alerts_message_length",
        "alerts",
        "message IS NULL OR LENGTH(message) <= 10000",
    )
    # M63: token hashes are exactly 64 hex chars (SHA-256). Existing rows all
    # fit, so the shrink is safe; the index on the column rebuilds in place.
    op.alter_column(
        "refresh_tokens",
        "token_hash",
        type_=sa.String(length=64),
        existing_type=sa.String(length=255),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "refresh_tokens",
        "token_hash",
        type_=sa.String(length=255),
        existing_type=sa.String(length=64),
        existing_nullable=False,
    )
    op.drop_constraint("ck_alerts_message_length", "alerts", type_="check")
    op.drop_index(
        "idx_alerts_unacked_triggered",
        table_name="alerts",
        postgresql_where=sa.text("acknowledged_at IS NULL"),
    )
