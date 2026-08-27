"""add_audit_logs

Add the ``audit_logs`` table, which was declared in models but never created
by any migration (M90). Alembic-provisioned DBs lacked the table, so the first
writer (the settings-PATCH audit trail added for M13) would 500. Creating it
here keeps the model and the DB in sync.

Table columns mirror ``models.setting.AuditLog``.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-27

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("entity_type", sa.String(length=50), nullable=False),
        sa.Column("entity_id", sa.String(length=100), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=True),
        sa.Column("changed_by", sa.Integer(), nullable=True),
        sa.Column(
            "changed_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["changed_by"],
            ["users.id"],
            name=op.f("audit_logs_changed_by_fkey"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("audit_logs_pkey")),
    )


def downgrade() -> None:
    op.drop_table("audit_logs")
