"""vscu device state and call log

Revision ID: f3a1c9d02b7e
Revises: aaddab8826f4
Create Date: 2026-09-01 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "f3a1c9d02b7e"
down_revision: Union[str, Sequence[str], None] = "aaddab8826f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "vscu_device_state",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("branch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("base_url", sa.String(), nullable=False),
        sa.Column("connect_timeout_seconds", sa.Float(), nullable=True),
        sa.Column("read_timeout_seconds", sa.Float(), nullable=True),
        sa.Column("dvc_srl_no", sa.String(), nullable=True),
        sa.Column("sdc_id", sa.String(), nullable=True),
        sa.Column("mrc_no", sa.String(), nullable=True),
        sa.Column("dvc_id", sa.String(), nullable=True),
        sa.Column("vat_ty_cd", sa.String(), nullable=True),
        sa.Column("taxpr_nm", sa.String(), nullable=True),
        sa.Column("bhf_nm", sa.String(), nullable=True),
        sa.Column("initialized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sequence_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_known_invc_no", sa.BigInteger(), nullable=True),
        sa.Column("last_known_sale_rcpt_no", sa.BigInteger(), nullable=True),
        sa.Column("last_successful_call_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["branch_id"], ["branches.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("branch_id"),
    )
    op.create_index(
        op.f("ix_vscu_device_state_branch_id"), "vscu_device_state", ["branch_id"], unique=True
    )

    op.create_table(
        "vscu_call_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("branch_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("endpoint", sa.String(), nullable=False),
        sa.Column("request_hash", sa.String(), nullable=False),
        sa.Column("result_cd", sa.String(), nullable=True),
        sa.Column("result_msg", sa.String(), nullable=True),
        sa.Column("result_dt", sa.String(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["branch_id"], ["branches.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_vscu_call_log_branch_id"), "vscu_call_log", ["branch_id"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_vscu_call_log_branch_id"), table_name="vscu_call_log")
    op.drop_table("vscu_call_log")
    op.drop_index(op.f("ix_vscu_device_state_branch_id"), table_name="vscu_device_state")
    op.drop_table("vscu_device_state")
