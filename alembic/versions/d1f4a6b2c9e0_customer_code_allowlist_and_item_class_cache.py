"""customer code allowlist and item class cache

Revision ID: d1f4a6b2c9e0
Revises: 878fe0715698
Create Date: 2026-09-18

Two new, empty tables — no backfill needed, no risk to existing data:
  - kra_item_classes: cache for KRA's item-classification list (spec §6.3), previously never
    persisted anywhere (only called live via KraAdapter.get_item_class_list).
  - customer_code_allowlist: per-Customer restriction on which KRA reference codes (tax-type/
    packaging-unit/quantity-unit/nation/item-classification) a connector may submit. Absence
    of rows for a (customer_id, classification) pair means unrestricted, so every existing
    Customer stays exactly as permissive as before this migration.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d1f4a6b2c9e0"
down_revision: str | None = "878fe0715698"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "kra_item_classes",
        sa.Column("item_cls_cd", sa.String(), nullable=False),
        sa.Column("item_cls_nm", sa.String(), nullable=True),
        sa.Column("item_cls_lvl", sa.String(), nullable=True),
        sa.Column("tax_ty_cd", sa.String(), nullable=True),
        sa.Column("mjr_tg_yn", sa.CHAR(length=1), nullable=True),
        sa.Column("use_yn", sa.CHAR(length=1), nullable=True),
        sa.Column(
            "refreshed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("item_cls_cd"),
    )
    op.create_table(
        "customer_code_allowlist",
        sa.Column("customer_id", sa.UUID(), nullable=False),
        sa.Column("classification", sa.String(), nullable=False),
        sa.Column("code", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.PrimaryKeyConstraint("customer_id", "classification", "code"),
    )
    # No separate index on customer_id: it's already the leading column of the composite
    # primary key above, so Postgres serves a plain "WHERE customer_id = ..." lookup (the only
    # access pattern code_allowlist_service.py uses) from that index's leftmost prefix.


def downgrade() -> None:
    op.drop_table("customer_code_allowlist")
    op.drop_table("kra_item_classes")
