"""client call log

Revision ID: 364b731c650a
Revises: a3d7e9f2c4b1
Create Date: 2026-11-09

Adds client_call_log — a persisted record of every inbound HTTP call this gateway received
and what it answered, written by AuditLogMiddleware alongside its existing stdout-only
logging. Distinct from kra_call_log/vscu_call_log (this gateway's own calls TO KRA/VSCU).
Nullable client_id since not every request carries a valid/decodable bearer token (admin
calls, unauthenticated calls that get rejected before routing, etc.).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "364b731c650a"
down_revision: str | None = "a3d7e9f2c4b1"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "client_call_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("method", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("request_body", postgresql.JSONB(), nullable=True),
        sa.Column("response_body", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_client_call_log_request_id"), "client_call_log", ["request_id"], unique=False
    )
    op.create_index(
        op.f("ix_client_call_log_client_id"), "client_call_log", ["client_id"], unique=False
    )
    op.create_index(
        op.f("ix_client_call_log_created_at"), "client_call_log", ["created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_client_call_log_created_at"), table_name="client_call_log")
    op.drop_index(op.f("ix_client_call_log_client_id"), table_name="client_call_log")
    op.drop_index(op.f("ix_client_call_log_request_id"), table_name="client_call_log")
    op.drop_table("client_call_log")
