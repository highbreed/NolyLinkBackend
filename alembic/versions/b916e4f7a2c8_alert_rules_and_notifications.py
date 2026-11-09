"""alert rules and notifications

Revision ID: b916e4f7a2c8
Revises: 7f2c81a9e3d4
Create Date: 2026-11-09

Adds alert_rules (threshold rules over metrics_service.KNOWN_METRICS) and notifications_sent
(one row per firing, written by app/workers/alert_evaluator.py). scope_customer_id/
scope_branch_code are kept as two columns rather than admin_portal_design.md §7's single
`scope` text column — the frontend's AlertRule/AlertRuleInput types already committed to the
split version.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b916e4f7a2c8"
down_revision: str | None = "7f2c81a9e3d4"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "alert_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("metric", sa.String(), nullable=False),
        sa.Column("scope_customer_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("scope_branch_code", sa.String(), nullable=True),
        sa.Column("condition", sa.String(), nullable=False),
        sa.Column("threshold", sa.Numeric(), nullable=False),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("channel_target", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
        sa.ForeignKeyConstraint(["scope_customer_id"], ["customers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "notifications_sent",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("alert_rule_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "fired_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metric_value", sa.Numeric(), nullable=False),
        sa.Column("summary", sa.String(), nullable=False),
        sa.Column("delivered", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("delivery_error", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["alert_rule_id"], ["alert_rules.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_notifications_sent_alert_rule_id"),
        "notifications_sent",
        ["alert_rule_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_notifications_sent_alert_rule_id"), table_name="notifications_sent")
    op.drop_table("notifications_sent")
    op.drop_table("alert_rules")
