"""admin audit log

Revision ID: 7f2c81a9e3d4
Revises: 364b731c650a
Create Date: 2026-11-09

Adds admin_audit_log — a record of every admin action ("admin X suspended customer Y"),
written by app/services/admin_audit.py alongside the existing stdout-only logger.info() calls
scattered across admin.py/billing.py/customers.py/device.py. Distinct from client_call_log
(what SAP/ERP clients sent us, not what an admin did).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "7f2c81a9e3d4"
down_revision: str | None = "364b731c650a"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "admin_audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("admin_user_email", sa.String(), nullable=True),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("target_type", sa.String(), nullable=False),
        sa.Column("target_id", sa.String(), nullable=False),
        sa.Column("before_state", postgresql.JSONB(), nullable=True),
        sa.Column("after_state", postgresql.JSONB(), nullable=True),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("ip_address", sa.String(), nullable=True),
        sa.Column("request_id", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_admin_audit_log_target_type"), "admin_audit_log", ["target_type"], unique=False
    )
    op.create_index(
        op.f("ix_admin_audit_log_target_id"), "admin_audit_log", ["target_id"], unique=False
    )
    op.create_index(
        op.f("ix_admin_audit_log_created_at"), "admin_audit_log", ["created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_admin_audit_log_created_at"), table_name="admin_audit_log")
    op.drop_index(op.f("ix_admin_audit_log_target_id"), table_name="admin_audit_log")
    op.drop_index(op.f("ix_admin_audit_log_target_type"), table_name="admin_audit_log")
    op.drop_table("admin_audit_log")
