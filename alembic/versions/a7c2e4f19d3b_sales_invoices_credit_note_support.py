"""sales invoices credit note support

Revision ID: a7c2e4f19d3b
Revises: f3a1c9d02b7e
Create Date: 2026-09-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7c2e4f19d3b'
down_revision: Union[str, Sequence[str], None] = 'f3a1c9d02b7e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'sales_invoices',
        sa.Column('transaction_kind', sa.String(), server_default='SALE', nullable=False),
    )
    op.add_column('sales_invoices', sa.Column('original_ref', sa.String(), nullable=True))
    op.drop_constraint(
        'uq_sales_invoices_branch_id_external_ref', 'sales_invoices', type_='unique'
    )
    op.create_unique_constraint(
        'uq_sales_invoices_branch_id_external_ref_transaction_kind',
        'sales_invoices',
        ['branch_id', 'external_ref', 'transaction_kind'],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(
        'uq_sales_invoices_branch_id_external_ref_transaction_kind',
        'sales_invoices',
        type_='unique',
    )
    op.create_unique_constraint(
        'uq_sales_invoices_branch_id_external_ref', 'sales_invoices', ['branch_id', 'external_ref']
    )
    op.drop_column('sales_invoices', 'original_ref')
    op.drop_column('sales_invoices', 'transaction_kind')
