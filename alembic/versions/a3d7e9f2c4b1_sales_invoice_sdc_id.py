"""sales invoice kra_sdc_id

Revision ID: a3d7e9f2c4b1
Revises: f8a1c6d3b9e2
Create Date: 2026-10-16

Adds SalesInvoice.kra_sdc_id — the receipt's "CU ID" (TIS-OSCU-VSCU-v2.0 spec §6.23.3),
needed alongside kra_cur_rcpt_no to build the "CU Invoice Number" (§6.23.4) KRA requires
printed on every receipt. Nullable, no backfill: existing rows simply have no CU ID recorded
(it was never captured before this), same as any other field added after the fact.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a3d7e9f2c4b1"
down_revision: str | None = "f8a1c6d3b9e2"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sales_invoices", sa.Column("kra_sdc_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("sales_invoices", "kra_sdc_id")
