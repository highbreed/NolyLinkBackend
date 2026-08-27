"""rename clients api_key_hash to client_secret_hash

Revision ID: 52554a01802b
Revises: c883095591f9
Create Date: 2026-08-27 13:52:17.499730

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '52554a01802b'
down_revision: Union[str, Sequence[str], None] = 'c883095591f9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column('clients', 'api_key_hash', new_column_name='client_secret_hash')


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column('clients', 'client_secret_hash', new_column_name='api_key_hash')
