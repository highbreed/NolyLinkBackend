"""Customer subscription/invoice management and access enforcement — this gateway's own
tenant billing, entirely outside the KRA spec (which has no concept of "customer" at all;
that's this project's own multi-tenant layer from Phase 9).

Keyed by customer_id, not client_id (2026-09-01): billing is a property of the paying
business (Customer), not of any one of its OAuth2 connectors (Client) — see Customer's
docstring in app/db/models.py. A Customer with several Clients is billed once, not per
connector.

Internal tracking only: nothing here calls a payment processor. An admin marks invoices paid
and subscriptions suspended/reactivated directly via app/api/v1/endpoints/billing.py. Wiring
a real processor (Stripe, M-Pesa/Daraja, ...) needs real API credentials from ops first — the
same policy this build followed throughout for KRA sandbox access, not a gap specific to
billing.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.models import Customer, Invoice, Subscription
from app.services.customer_service import CustomerNotFoundError as CustomerNotFoundError


class SubscriptionNotFoundError(Exception):
    pass


class InvoiceNotFoundError(Exception):
    pass


class SubscriptionBlockedError(Exception):
    """Raised by enforce_active_subscription; the API layer maps this to HTTP 402."""


def _as_utc(dt: datetime) -> datetime:
    """Pydantic accepts naive datetimes; every stored timestamp here must be tz-aware (the
    grace-period comparison in enforce_active_subscription mixes stored values with
    datetime.now(UTC), which raises TypeError against a naive value). Naive input is assumed
    UTC rather than rejected outright, since that's the overwhelmingly likely intent for an
    admin-only billing endpoint.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def get_subscription(db: AsyncSession, customer_id: uuid.UUID) -> Subscription | None:
    result: Subscription | None = await db.scalar(
        select(Subscription).where(Subscription.customer_id == customer_id)
    )
    return result


async def upsert_subscription(
    db: AsyncSession,
    customer_id: uuid.UUID,
    plan_name: str,
    current_period_end: datetime,
    grace_period_days: int | None,
) -> Subscription:
    """Creates the subscription if none exists, else renews plan/period on the existing one.

    Deliberately leaves `status` untouched on an update — renewing the paid-through date must
    never silently lift an explicit SUSPENDED/CANCELLED (use reactivate_subscription for
    that), since a payment landing for the wrong reason shouldn't undo a fraud suspension.
    """
    if await db.get(Customer, customer_id) is None:
        raise CustomerNotFoundError(f"No customer with id={customer_id}")

    current_period_end = _as_utc(current_period_end)

    existing = await get_subscription(db, customer_id)
    if existing is not None:
        existing.plan_name = plan_name
        existing.current_period_end = current_period_end
        existing.grace_period_days = grace_period_days
        await db.flush()
        return existing

    subscription = Subscription(
        customer_id=customer_id,
        plan_name=plan_name,
        current_period_end=current_period_end,
        grace_period_days=grace_period_days,
    )
    db.add(subscription)
    await db.flush()
    return subscription


async def suspend_subscription(
    db: AsyncSession, customer_id: uuid.UUID, reason: str
) -> Subscription:
    subscription = await get_subscription(db, customer_id)
    if subscription is None:
        raise SubscriptionNotFoundError(f"No subscription on file for customer_id={customer_id}")
    subscription.status = "SUSPENDED"
    subscription.suspended_at = datetime.now(UTC)
    subscription.suspended_reason = reason
    await db.flush()
    return subscription


async def reactivate_subscription(db: AsyncSession, customer_id: uuid.UUID) -> Subscription:
    subscription = await get_subscription(db, customer_id)
    if subscription is None:
        raise SubscriptionNotFoundError(f"No subscription on file for customer_id={customer_id}")
    subscription.status = "ACTIVE"
    subscription.suspended_at = None
    subscription.suspended_reason = None
    await db.flush()
    return subscription


async def cancel_subscription(db: AsyncSession, customer_id: uuid.UUID) -> Subscription:
    subscription = await get_subscription(db, customer_id)
    if subscription is None:
        raise SubscriptionNotFoundError(f"No subscription on file for customer_id={customer_id}")
    subscription.status = "CANCELLED"
    await db.flush()
    return subscription


async def create_invoice(
    db: AsyncSession,
    customer_id: uuid.UUID,
    period_start: datetime,
    period_end: datetime,
    amount: Decimal,
    currency: str,
    notes: str | None,
) -> Invoice:
    if await db.get(Customer, customer_id) is None:
        raise CustomerNotFoundError(f"No customer with id={customer_id}")

    invoice = Invoice(
        customer_id=customer_id,
        period_start=_as_utc(period_start),
        period_end=_as_utc(period_end),
        amount=amount,
        currency=currency,
        notes=notes,
    )
    db.add(invoice)
    await db.flush()
    return invoice


async def list_invoices(db: AsyncSession, customer_id: uuid.UUID) -> list[Invoice]:
    result = await db.execute(
        select(Invoice).where(Invoice.customer_id == customer_id).order_by(Invoice.issued_at.desc())
    )
    return list(result.scalars().all())


async def mark_invoice_paid(
    db: AsyncSession, invoice_id: uuid.UUID, extend_subscription_to: datetime | None
) -> Invoice:
    """extend_subscription_to is a convenience: marking an invoice paid commonly coincides
    with renewing the subscription for the period it covers, so this can do both in one call.
    Omit it to just record payment without touching current_period_end.
    """
    invoice = await db.get(Invoice, invoice_id)
    if invoice is None:
        raise InvoiceNotFoundError(f"No invoice with id={invoice_id}")

    invoice.status = "PAID"
    invoice.paid_at = datetime.now(UTC)

    if extend_subscription_to is not None:
        subscription = await get_subscription(db, invoice.customer_id)
        if subscription is not None:
            subscription.current_period_end = _as_utc(extend_subscription_to)

    await db.flush()
    return invoice


async def enforce_active_subscription(
    db: AsyncSession, customer: Customer, settings: Settings | None = None
) -> None:
    """Called from client_auth.get_current_client (via client.customer) on every authenticated
    request. A no-op (billing_enforcement_enabled defaults False) until ops has backfilled
    Subscription rows for every existing customer and deliberately turns this on — otherwise
    every already-onboarded customer would be locked out the moment this shipped, with no
    subscription row to check.

    Once enabled: no subscription on file, SUSPENDED, CANCELLED, or past
    current_period_end + grace_period_days all raise SubscriptionBlockedError -> HTTP 402.
    """
    settings = settings or get_settings()
    if not settings.billing_enforcement_enabled:
        return

    subscription = await get_subscription(db, customer.id)
    if subscription is None:
        raise SubscriptionBlockedError(
            f"Customer {customer.name!r} has no billing subscription on file"
        )

    if subscription.status == "CANCELLED":
        raise SubscriptionBlockedError(f"Customer {customer.name!r}'s subscription is cancelled")

    if subscription.status == "SUSPENDED":
        reason = subscription.suspended_reason or "payment overdue"
        raise SubscriptionBlockedError(
            f"Customer {customer.name!r}'s subscription is suspended: {reason}"
        )

    grace_days = (
        subscription.grace_period_days
        if subscription.grace_period_days is not None
        else settings.billing_grace_period_days
    )
    grace_deadline = subscription.current_period_end + timedelta(days=grace_days)
    if datetime.now(UTC) > grace_deadline:
        raise SubscriptionBlockedError(
            f"Customer {customer.name!r}'s subscription lapsed on "
            f"{subscription.current_period_end.date()} and its {grace_days}-day grace period "
            "has ended"
        )
