"""Admin-only client billing endpoints: subscriptions + invoices (internal tracking only —
see app/services/billing_service.py's module docstring on payment-processor scope).

Gated by the same X-Admin-Api-Key mechanism as device init (app/core/admin_auth.py), not
Phase 9's client OAuth2 system — a tenant must never be able to see or touch its own billing
enforcement state through this surface.
"""

import logging
import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_auth import require_admin
from app.db.models import Invoice, Subscription
from app.db.session import get_db
from app.services import billing_service

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_admin)])


class SubscriptionUpsertRequest(BaseModel):
    plan_name: str = "STANDARD"
    current_period_end: datetime
    grace_period_days: int | None = None


class SubscriptionOut(BaseModel):
    client_id: uuid.UUID
    plan_name: str
    status: str
    current_period_end: datetime
    grace_period_days: int | None
    suspended_at: datetime | None
    suspended_reason: str | None


def _to_subscription_out(sub: Subscription) -> SubscriptionOut:
    return SubscriptionOut(
        client_id=sub.client_id,
        plan_name=sub.plan_name,
        status=sub.status,
        current_period_end=sub.current_period_end,
        grace_period_days=sub.grace_period_days,
        suspended_at=sub.suspended_at,
        suspended_reason=sub.suspended_reason,
    )


@router.put("/subscriptions/{client_id}", response_model=SubscriptionOut, status_code=200)
async def upsert_subscription(
    client_id: uuid.UUID,
    payload: SubscriptionUpsertRequest,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> SubscriptionOut:
    """Creates the subscription if none exists, else renews plan/period on the existing one.
    Never touches status — a payment landing can't silently lift a SUSPENDED/CANCELLED state;
    use the explicit reactivate endpoint below for that.
    """
    try:
        sub = await billing_service.upsert_subscription(
            db, client_id, payload.plan_name, payload.current_period_end, payload.grace_period_days
        )
        await db.commit()
    except billing_service.ClientNotFoundError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    logger.info("Subscription for client_id=%s upserted by %s", client_id, admin_actor)
    return _to_subscription_out(sub)


@router.get("/subscriptions/{client_id}", response_model=SubscriptionOut)
async def get_subscription(
    client_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> SubscriptionOut:
    sub = await billing_service.get_subscription(db, client_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="No subscription on file for this client")
    return _to_subscription_out(sub)


class SuspendRequest(BaseModel):
    reason: str


@router.post("/subscriptions/{client_id}/suspend", response_model=SubscriptionOut)
async def suspend_subscription(
    client_id: uuid.UUID,
    payload: SuspendRequest,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> SubscriptionOut:
    try:
        sub = await billing_service.suspend_subscription(db, client_id, payload.reason)
        await db.commit()
    except billing_service.SubscriptionNotFoundError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    logger.info(
        "Subscription for client_id=%s suspended by %s (reason=%r)",
        client_id,
        admin_actor,
        payload.reason,
    )
    return _to_subscription_out(sub)


@router.post("/subscriptions/{client_id}/reactivate", response_model=SubscriptionOut)
async def reactivate_subscription(
    client_id: uuid.UUID,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> SubscriptionOut:
    try:
        sub = await billing_service.reactivate_subscription(db, client_id)
        await db.commit()
    except billing_service.SubscriptionNotFoundError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    logger.info("Subscription for client_id=%s reactivated by %s", client_id, admin_actor)
    return _to_subscription_out(sub)


@router.post("/subscriptions/{client_id}/cancel", response_model=SubscriptionOut)
async def cancel_subscription(
    client_id: uuid.UUID,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> SubscriptionOut:
    try:
        sub = await billing_service.cancel_subscription(db, client_id)
        await db.commit()
    except billing_service.SubscriptionNotFoundError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    logger.info("Subscription for client_id=%s cancelled by %s", client_id, admin_actor)
    return _to_subscription_out(sub)


class InvoiceCreateRequest(BaseModel):
    period_start: datetime
    period_end: datetime
    amount: Decimal
    currency: str = "KES"
    notes: str | None = None


class InvoiceOut(BaseModel):
    id: uuid.UUID
    client_id: uuid.UUID
    period_start: datetime
    period_end: datetime
    amount: Decimal
    currency: str
    status: str
    issued_at: datetime
    paid_at: datetime | None
    notes: str | None


def _to_invoice_out(inv: Invoice) -> InvoiceOut:
    return InvoiceOut(
        id=inv.id,
        client_id=inv.client_id,
        period_start=inv.period_start,
        period_end=inv.period_end,
        amount=inv.amount,
        currency=inv.currency,
        status=inv.status,
        issued_at=inv.issued_at,
        paid_at=inv.paid_at,
        notes=inv.notes,
    )


@router.post("/clients/{client_id}/invoices", response_model=InvoiceOut, status_code=201)
async def create_invoice(
    client_id: uuid.UUID,
    payload: InvoiceCreateRequest,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> InvoiceOut:
    try:
        inv = await billing_service.create_invoice(
            db,
            client_id,
            payload.period_start,
            payload.period_end,
            payload.amount,
            payload.currency,
            payload.notes,
        )
        await db.commit()
    except billing_service.ClientNotFoundError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    logger.info("Invoice for client_id=%s created by %s", client_id, admin_actor)
    return _to_invoice_out(inv)


@router.get("/clients/{client_id}/invoices", response_model=list[InvoiceOut])
async def list_invoices(
    client_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> list[InvoiceOut]:
    invoices = await billing_service.list_invoices(db, client_id)
    return [_to_invoice_out(inv) for inv in invoices]


class MarkPaidRequest(BaseModel):
    # Convenience: marking an invoice paid commonly coincides with renewing the subscription
    # for the period it covers. Omit to just record payment without touching the subscription.
    extend_subscription_to: datetime | None = None


@router.post("/invoices/{invoice_id}/mark-paid", response_model=InvoiceOut)
async def mark_invoice_paid(
    invoice_id: uuid.UUID,
    payload: MarkPaidRequest,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> InvoiceOut:
    try:
        inv = await billing_service.mark_invoice_paid(
            db, invoice_id, payload.extend_subscription_to
        )
        await db.commit()
    except billing_service.InvoiceNotFoundError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    logger.info("Invoice %s marked paid by %s", invoice_id, admin_actor)
    return _to_invoice_out(inv)
