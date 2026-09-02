"""add_password_reset_tokens_user_id_index

The ``PasswordResetToken`` model declares ``index=True`` on ``user_id`` but
migration 0008 never created it, so the forgot-password supersede
(``UPDATE ... WHERE user_id = ... AND used_at IS NULL``) ran as a seq-scan
and ``alembic check`` reported drift. This migration closes the gap.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-02

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        op.f("password_reset_tokens_user_id_idx"),
        "password_reset_tokens",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("password_reset_tokens_user_id_idx"),
        table_name="password_reset_tokens",
    )
