"""Onboarding: create/manage a paying Customer and the OAuth2 Client connector(s) that
integrate on its behalf (see Customer/Client docstrings in app/db/models.py, 2026-09-01).

Replaces what docs/client_onboarding_and_monitoring.md previously documented as a manual raw
SQL insert — the first step of onboarding a new customer is now POST /v1/customers followed by
POST /v1/customers/{customer_id}/connectors, both admin-only.
"""

import secrets
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Branch, Client, Customer
from app.services import code_allowlist_service
from app.services.secret_hashing import hash_client_secret


class CustomerNotFoundError(Exception):
    pass


class ConnectorNotFoundError(Exception):
    pass


class UnknownBranchCodesError(Exception):
    def __init__(self, branch_codes: list[str]):
        self.branch_codes = branch_codes
        super().__init__(f"Unknown branch_code(s): {', '.join(branch_codes)}")


async def resolve_branch_codes(db: AsyncSession, branch_codes: list[str]) -> list[uuid.UUID]:
    """Admin-facing requests name branches by branch_code (the identifier used everywhere else
    in this API), never raw UUIDs — this resolves them to the ids Customer.allowed_branch_ids
    actually stores. Raises if any code doesn't exist, rather than silently dropping it.
    """
    if not branch_codes:
        return []
    result = await db.execute(select(Branch).where(Branch.branch_code.in_(branch_codes)))
    found = {b.branch_code: b.id for b in result.scalars().all()}
    missing = [code for code in branch_codes if code not in found]
    if missing:
        raise UnknownBranchCodesError(missing)
    return [found[code] for code in branch_codes]


async def create_customer(db: AsyncSession, name: str, branch_ids: list[uuid.UUID]) -> Customer:
    customer = Customer(name=name, allowed_branch_ids=branch_ids)
    db.add(customer)
    await db.flush()
    return customer


async def list_customers(db: AsyncSession) -> list[Customer]:
    result = await db.execute(select(Customer).order_by(Customer.created_at.desc()))
    return list(result.scalars().all())


async def get_customer(db: AsyncSession, customer_id: uuid.UUID) -> Customer | None:
    return await db.get(Customer, customer_id)


async def update_customer(
    db: AsyncSession,
    customer_id: uuid.UUID,
    name: str | None,
    branch_ids: list[uuid.UUID] | None,
) -> Customer:
    customer = await db.get(Customer, customer_id)
    if customer is None:
        raise CustomerNotFoundError(f"No customer with id={customer_id}")
    if name is not None:
        customer.name = name
    if branch_ids is not None:
        customer.allowed_branch_ids = branch_ids
    await db.flush()
    return customer


async def create_connector(
    db: AsyncSession, customer_id: uuid.UUID, name: str
) -> tuple[Client, str]:
    """Returns (Client, plaintext_secret) — the plaintext secret exists only in this return
    value and is never persisted or logged; the caller must hand it to the integrator now,
    since it can't be recovered later (only client_secret_hash is stored).
    """
    if await db.get(Customer, customer_id) is None:
        raise CustomerNotFoundError(f"No customer with id={customer_id}")

    raw_secret = secrets.token_urlsafe(32)
    client = Client(
        customer_id=customer_id, name=name, client_secret_hash=hash_client_secret(raw_secret)
    )
    db.add(client)
    await db.flush()
    return client, raw_secret


async def list_connectors(db: AsyncSession, customer_id: uuid.UUID) -> list[Client]:
    result = await db.execute(
        select(Client).where(Client.customer_id == customer_id).order_by(Client.created_at.desc())
    )
    return list(result.scalars().all())


async def _get_connector(db: AsyncSession, customer_id: uuid.UUID, client_id: uuid.UUID) -> Client:
    client = await db.scalar(
        select(Client).where(Client.id == client_id, Client.customer_id == customer_id)
    )
    if client is None:
        raise ConnectorNotFoundError(
            f"No connector with id={client_id} under customer_id={customer_id}"
        )
    return client


async def revoke_connector(
    db: AsyncSession, customer_id: uuid.UUID, client_id: uuid.UUID
) -> Client:
    """Soft revoke (see Client.revoked_at's docstring in app/db/models.py) — idempotent:
    revoking an already-revoked connector is a no-op, not an error.
    """
    client = await _get_connector(db, customer_id, client_id)
    if client.revoked_at is None:
        client.revoked_at = datetime.now(UTC)
        await db.flush()
    return client


async def rotate_connector_secret(
    db: AsyncSession, customer_id: uuid.UUID, client_id: uuid.UUID
) -> tuple[Client, str]:
    """Same (Client, plaintext_secret) contract as create_connector, for the SAME client_id —
    the integrator keeps its client_id and only needs to update the secret it holds. Works
    independently of revoked_at: rotating a revoked connector's secret doesn't un-revoke it.
    """
    client = await _get_connector(db, customer_id, client_id)
    raw_secret = secrets.token_urlsafe(32)
    client.client_secret_hash = hash_client_secret(raw_secret)
    await db.flush()
    return client, raw_secret


async def get_code_allowlist(db: AsyncSession, customer_id: uuid.UUID) -> dict[str, list[str]]:
    if await db.get(Customer, customer_id) is None:
        raise CustomerNotFoundError(f"No customer with id={customer_id}")
    return await code_allowlist_service.get_code_allowlist(db, customer_id)


async def set_code_allowlist(
    db: AsyncSession, customer_id: uuid.UUID, classification: str, codes: list[str]
) -> None:
    if await db.get(Customer, customer_id) is None:
        raise CustomerNotFoundError(f"No customer with id={customer_id}")
    await code_allowlist_service.set_code_allowlist(db, customer_id, classification, codes)
