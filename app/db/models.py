"""SQLAlchemy models: branches, clients, items, kra_code_lists, kra_sync_state,
idempotency_keys, outbox, kra_call_log, sales_invoices (spec §17). Implemented in Phase 1.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared declarative base; Alembic's env.py targets Base.metadata for autogenerate."""
