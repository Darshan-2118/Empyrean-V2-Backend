"""add_refresh_token_expiry_index

L67 — ``refresh_tokens.expires_at`` is declared ``index=True`` in the model
(models/refresh_token.py) but no migration ever created the index, so the
daily ``refresh_token_cleanup`` (``WHERE expires_at < now()``) seq-scans and
the next ``alembic --autogenerate`` would try to add it. Create the index
with the models' MetaData naming convention (``%(column_0_label)s_idx``) so
the model stays the source of truth.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-28

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # L67: index refresh_tokens.expires_at for the cleanup job's
    # WHERE expires_at < now() filter.
    op.create_index(
        op.f("refresh_tokens_expires_at_idx"),
        "refresh_tokens",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("refresh_tokens_expires_at_idx"), table_name="refresh_tokens")
