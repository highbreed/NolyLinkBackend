"""client revoked_at

Revision ID: f8a1c6d3b9e2
Revises: e2b6f7c1a4d3
Create Date: 2026-10-15

Adds Client.revoked_at for soft-revoking a connector credential (POST /v1/customers/
{customer_id}/connectors/{client_id}/revoke) — not a delete, since IdempotencyKey.client_id is
a real FK to this table and a revoked connector may have genuine fiscal history behind it.
Nullable, no default: every existing Client row is unrevoked (NULL) after this migration,
exactly as before it existed.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f8a1c6d3b9e2"
down_revision: str | None = "e2b6f7c1a4d3"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("clients", sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("clients", "revoked_at")
