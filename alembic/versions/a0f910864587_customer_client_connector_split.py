"""customer/client(connector) split

Revision ID: a0f910864587
Revises: a7c2e4f19d3b
Create Date: 2026-09-01

Introduces Customer (the paying business — owns branch access and billing) as distinct from
Client (one OAuth2 credential/connector integrating on a Customer's behalf; a Customer can
have several). Previously these were conflated into a single `clients` row that doubled as
both the OAuth2 credential AND the billing/branch-access tenant.

Backfill strategy: since every existing Client today implies exactly one paying business
(no multi-connector customer exists yet), this creates one Customer per existing Client,
carrying over its name and allowed_branch_ids, then points that Client at the new Customer
and repoints Subscription/Invoice from client_id to the new customer_id. The `clients` table
itself is left in place (id/secret_hash/created_at unchanged) — every existing OAuth2
credential and every IdempotencyKey FK referencing it keeps working untouched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a0f910864587"
down_revision: str | None = "a7c2e4f19d3b"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column(
            "allowed_branch_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # One Customer per existing Client, carrying over name + allowed_branch_ids verbatim.
    op.execute(
        """
        INSERT INTO customers (id, name, allowed_branch_ids, created_at)
        SELECT gen_random_uuid(), name, allowed_branch_ids, created_at FROM clients
        """
    )

    op.add_column("clients", sa.Column("customer_id", postgresql.UUID(as_uuid=True)))
    # Match each client back to "its" backfilled customer by identical name — safe only
    # because the INSERT above ran in the same transaction and names were unique per client
    # at this point in time (pre-refactor, one client == one tenant).
    op.execute(
        """
        UPDATE clients SET customer_id = customers.id
        FROM customers WHERE customers.name = clients.name
        """
    )
    op.alter_column("clients", "customer_id", nullable=False)
    op.create_foreign_key(
        "fk_clients_customer_id_customers", "clients", "customers", ["customer_id"], ["id"]
    )
    op.create_index("ix_clients_customer_id", "clients", ["customer_id"])
    op.drop_column("clients", "allowed_branch_ids")

    for table in ("subscriptions", "invoices"):
        op.add_column(
            table, sa.Column("customer_id", postgresql.UUID(as_uuid=True))
        )
        op.execute(
            f"""
            UPDATE {table} SET customer_id = clients.customer_id
            FROM clients WHERE clients.id = {table}.client_id
            """
        )
        op.alter_column(table, "customer_id", nullable=False)
        op.drop_constraint(f"{table}_client_id_fkey", table, type_="foreignkey")
        op.drop_column(table, "client_id")
        op.create_foreign_key(
            f"fk_{table}_customer_id_customers", table, "customers", ["customer_id"], ["id"]
        )

    op.create_index("ix_subscriptions_customer_id", "subscriptions", ["customer_id"], unique=True)
    op.create_index("ix_invoices_customer_id", "invoices", ["customer_id"])


def downgrade() -> None:
    op.drop_index("ix_invoices_customer_id", table_name="invoices")
    op.drop_index("ix_subscriptions_customer_id", table_name="subscriptions")

    for table in ("subscriptions", "invoices"):
        op.add_column(table, sa.Column("client_id", postgresql.UUID(as_uuid=True)))
        op.execute(
            f"""
            UPDATE {table} SET client_id = clients.id
            FROM clients WHERE clients.customer_id = {table}.customer_id
            """
        )
        op.alter_column(table, "client_id", nullable=False)
        op.drop_constraint(f"fk_{table}_customer_id_customers", table, type_="foreignkey")
        op.drop_column(table, "customer_id")
        op.create_foreign_key(f"{table}_client_id_fkey", table, "clients", ["client_id"], ["id"])

    op.add_column(
        "clients",
        sa.Column(
            "allowed_branch_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
        ),
    )
    op.execute(
        """
        UPDATE clients SET allowed_branch_ids = customers.allowed_branch_ids
        FROM customers WHERE customers.id = clients.customer_id
        """
    )
    op.alter_column("clients", "allowed_branch_ids", nullable=False)
    op.drop_index("ix_clients_customer_id", table_name="clients")
    op.drop_constraint("fk_clients_customer_id_customers", "clients", type_="foreignkey")
    op.drop_column("clients", "customer_id")

    op.drop_table("customers")
