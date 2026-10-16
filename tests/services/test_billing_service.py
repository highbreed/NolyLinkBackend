"""Tests for billing_service: subscription/invoice CRUD and the grace-period enforcement
math (app/services/billing_service.py). This is this gateway's own tenant billing, entirely
outside the KRA spec.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.db.models import Customer
from app.services import billing_service

ENFORCED = Settings(billing_enforcement_enabled=True, billing_grace_period_days=7)
NOT_ENFORCED = Settings(billing_enforcement_enabled=False)


@pytest.fixture
async def registered_customer(db_session) -> Customer:
    """Billing is a property of the paying business (Customer), not any one of its OAuth2
    connectors (Client) — these tests call billing_service directly, so no Client row is
    needed at all.
    """
    customer = Customer(name="SAP-TEST", allowed_branch_ids=[])
    db_session.add(customer)
    await db_session.flush()
    return customer


async def test_enforcement_is_a_noop_when_disabled(db_session, registered_customer):
    # No subscription row at all — would block if enforcement were on.
    await billing_service.enforce_active_subscription(db_session, registered_customer, NOT_ENFORCED)


async def test_no_subscription_on_file_blocks_when_enforced(db_session, registered_customer):
    with pytest.raises(billing_service.SubscriptionBlockedError, match="no billing subscription"):
        await billing_service.enforce_active_subscription(db_session, registered_customer, ENFORCED)


async def test_active_subscription_within_period_passes(db_session, registered_customer):
    await billing_service.upsert_subscription(
        db_session,
        registered_customer.id,
        "STANDARD",
        datetime.now(UTC) + timedelta(days=10),
        grace_period_days=None,
    )
    await billing_service.enforce_active_subscription(db_session, registered_customer, ENFORCED)


async def test_lapsed_but_within_grace_period_still_passes(db_session, registered_customer):
    await billing_service.upsert_subscription(
        db_session,
        registered_customer.id,
        "STANDARD",
        datetime.now(UTC) - timedelta(days=3),  # lapsed 3 days ago
        grace_period_days=None,  # falls back to ENFORCED.billing_grace_period_days=7
    )
    await billing_service.enforce_active_subscription(db_session, registered_customer, ENFORCED)


async def test_lapsed_past_grace_period_blocks(db_session, registered_customer):
    await billing_service.upsert_subscription(
        db_session,
        registered_customer.id,
        "STANDARD",
        datetime.now(UTC) - timedelta(days=10),  # lapsed 10 days ago, grace is 7
        grace_period_days=None,
    )
    with pytest.raises(billing_service.SubscriptionBlockedError, match="grace period"):
        await billing_service.enforce_active_subscription(db_session, registered_customer, ENFORCED)


async def test_per_subscription_grace_period_overrides_global_default(
    db_session, registered_customer
):
    await billing_service.upsert_subscription(
        db_session,
        registered_customer.id,
        "STANDARD",
        datetime.now(UTC) - timedelta(days=10),
        grace_period_days=30,  # overrides ENFORCED's global 7
    )
    await billing_service.enforce_active_subscription(db_session, registered_customer, ENFORCED)


async def test_suspended_blocks_regardless_of_period(db_session, registered_customer):
    await billing_service.upsert_subscription(
        db_session,
        registered_customer.id,
        "STANDARD",
        datetime.now(UTC) + timedelta(days=365),  # comfortably within period
        grace_period_days=None,
    )
    await billing_service.suspend_subscription(db_session, registered_customer.id, "chargeback")
    with pytest.raises(billing_service.SubscriptionBlockedError, match="chargeback"):
        await billing_service.enforce_active_subscription(db_session, registered_customer, ENFORCED)


async def test_renewing_period_does_not_lift_a_suspension(db_session, registered_customer):
    """A payment landing for the wrong reason (e.g. an old scheduled renewal) must never
    silently undo an explicit fraud/chargeback suspension.
    """
    await billing_service.upsert_subscription(
        db_session, registered_customer.id, "STANDARD", datetime.now(UTC) + timedelta(days=30), None
    )
    await billing_service.suspend_subscription(db_session, registered_customer.id, "fraud")

    await billing_service.upsert_subscription(
        db_session, registered_customer.id, "STANDARD", datetime.now(UTC) + timedelta(days=60), None
    )

    with pytest.raises(billing_service.SubscriptionBlockedError, match="fraud"):
        await billing_service.enforce_active_subscription(db_session, registered_customer, ENFORCED)


async def test_reactivate_clears_suspension(db_session, registered_customer):
    await billing_service.upsert_subscription(
        db_session, registered_customer.id, "STANDARD", datetime.now(UTC) + timedelta(days=30), None
    )
    await billing_service.suspend_subscription(db_session, registered_customer.id, "fraud")
    await billing_service.reactivate_subscription(db_session, registered_customer.id)

    await billing_service.enforce_active_subscription(db_session, registered_customer, ENFORCED)


async def test_cancelled_blocks(db_session, registered_customer):
    await billing_service.upsert_subscription(
        db_session, registered_customer.id, "STANDARD", datetime.now(UTC) + timedelta(days=30), None
    )
    await billing_service.cancel_subscription(db_session, registered_customer.id)
    with pytest.raises(billing_service.SubscriptionBlockedError, match="cancelled"):
        await billing_service.enforce_active_subscription(db_session, registered_customer, ENFORCED)


async def test_upsert_for_unknown_customer_raises(db_session):
    with pytest.raises(billing_service.CustomerNotFoundError):
        await billing_service.upsert_subscription(
            db_session, uuid.uuid4(), "STANDARD", datetime.now(UTC), None
        )


async def test_invoice_create_and_mark_paid_can_extend_subscription(
    db_session, registered_customer
):
    await billing_service.upsert_subscription(
        db_session, registered_customer.id, "STANDARD", datetime.now(UTC) + timedelta(days=1), None
    )
    invoice = await billing_service.create_invoice(
        db_session,
        registered_customer.id,
        datetime.now(UTC),
        datetime.now(UTC) + timedelta(days=30),
        amount=5000,
        currency="KES",
        notes=None,
    )
    assert invoice.status == "ISSUED"

    new_period_end = datetime.now(UTC) + timedelta(days=30)
    paid = await billing_service.mark_invoice_paid(db_session, invoice.id, new_period_end)
    assert paid.status == "PAID"
    assert paid.paid_at is not None

    sub = await billing_service.get_subscription(db_session, registered_customer.id)
    assert sub is not None
    assert sub.current_period_end == new_period_end


async def test_mark_paid_without_extend_leaves_subscription_untouched(
    db_session, registered_customer
):
    original_end = datetime.now(UTC) + timedelta(days=1)
    await billing_service.upsert_subscription(
        db_session, registered_customer.id, "STANDARD", original_end, None
    )
    invoice = await billing_service.create_invoice(
        db_session,
        registered_customer.id,
        datetime.now(UTC),
        datetime.now(UTC) + timedelta(days=30),
        amount=5000,
        currency="KES",
        notes=None,
    )
    await billing_service.mark_invoice_paid(db_session, invoice.id, None)

    sub = await billing_service.get_subscription(db_session, registered_customer.id)
    assert sub is not None
    assert sub.current_period_end == original_end


async def test_naive_datetime_input_is_treated_as_utc(db_session, registered_customer):
    """Pydantic accepts a naive datetime from an admin who forgot a timezone offset — this
    must not later crash the naive-vs-aware comparison in enforce_active_subscription.
    """
    naive_future = (datetime.now(UTC) + timedelta(days=10)).replace(tzinfo=None)
    await billing_service.upsert_subscription(
        db_session, registered_customer.id, "STANDARD", naive_future, None
    )
    await billing_service.enforce_active_subscription(db_session, registered_customer, ENFORCED)
