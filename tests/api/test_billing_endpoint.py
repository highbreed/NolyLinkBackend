"""API tests for /v1/billing (admin-only subscription/invoice management), plus the
integration proof that a blocked client's ordinary requests actually get 402 — the whole
point of client_auth.get_current_client's billing_service.enforce_active_subscription call.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings, get_settings
from app.db.models import Branch, Client
from app.db.session import get_db
from app.main import app
from app.services import billing_service
from app.services.client_auth import hash_client_secret
from tests.auth_helpers import bearer_header


@pytest.fixture
def client(db_session):
    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    try:
        transport = ASGITransport(app=app)
        yield AsyncClient(transport=transport, base_url="http://test")
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def admin_headers() -> dict[str, str]:
    admin_key = get_settings().admin_api_key
    assert admin_key, "ADMIN_API_KEY must be configured for this test run"
    return {"X-Admin-Api-Key": admin_key}


@pytest.fixture
async def registered_client(db_session) -> Client:
    c = Client(
        name="SAP-TEST", client_secret_hash=hash_client_secret("irrelevant"), allowed_branch_ids=[]
    )
    db_session.add(c)
    await db_session.flush()
    return c


async def test_billing_endpoints_require_admin_key(client, registered_client):
    async with client as c:
        response = await c.put(
            f"/v1/billing/subscriptions/{registered_client.id}",
            json={"current_period_end": "2030-01-01T00:00:00Z"},
        )
    assert response.status_code == 403


async def test_upsert_then_get_subscription(client, admin_headers, registered_client):
    async with client as c:
        create = await c.put(
            f"/v1/billing/subscriptions/{registered_client.id}",
            headers=admin_headers,
            json={"plan_name": "PREMIUM", "current_period_end": "2030-01-01T00:00:00Z"},
        )
        assert create.status_code == 200
        assert create.json()["status"] == "ACTIVE"
        assert create.json()["plan_name"] == "PREMIUM"

        fetched = await c.get(
            f"/v1/billing/subscriptions/{registered_client.id}", headers=admin_headers
        )
    assert fetched.status_code == 200
    assert fetched.json()["plan_name"] == "PREMIUM"


async def test_upsert_for_unknown_client_is_404(client, admin_headers):
    async with client as c:
        response = await c.put(
            f"/v1/billing/subscriptions/{uuid.uuid4()}",
            headers=admin_headers,
            json={"current_period_end": "2030-01-01T00:00:00Z"},
        )
    assert response.status_code == 404


async def test_suspend_then_reactivate(client, admin_headers, registered_client):
    async with client as c:
        await c.put(
            f"/v1/billing/subscriptions/{registered_client.id}",
            headers=admin_headers,
            json={"current_period_end": "2030-01-01T00:00:00Z"},
        )
        suspended = await c.post(
            f"/v1/billing/subscriptions/{registered_client.id}/suspend",
            headers=admin_headers,
            json={"reason": "overdue invoice"},
        )
        assert suspended.status_code == 200
        assert suspended.json()["status"] == "SUSPENDED"
        assert suspended.json()["suspended_reason"] == "overdue invoice"

        reactivated = await c.post(
            f"/v1/billing/subscriptions/{registered_client.id}/reactivate", headers=admin_headers
        )
    assert reactivated.status_code == 200
    assert reactivated.json()["status"] == "ACTIVE"
    assert reactivated.json()["suspended_reason"] is None


async def test_create_invoice_and_mark_paid_extends_subscription(
    client, admin_headers, registered_client
):
    async with client as c:
        await c.put(
            f"/v1/billing/subscriptions/{registered_client.id}",
            headers=admin_headers,
            json={"current_period_end": "2026-01-01T00:00:00Z"},
        )
        invoice = await c.post(
            f"/v1/billing/clients/{registered_client.id}/invoices",
            headers=admin_headers,
            json={
                "period_start": "2026-01-01T00:00:00Z",
                "period_end": "2026-02-01T00:00:00Z",
                "amount": "5000.00",
            },
        )
        assert invoice.status_code == 201
        assert invoice.json()["status"] == "ISSUED"
        invoice_id = invoice.json()["id"]

        paid = await c.post(
            f"/v1/billing/invoices/{invoice_id}/mark-paid",
            headers=admin_headers,
            json={"extend_subscription_to": "2026-02-01T00:00:00Z"},
        )
        assert paid.status_code == 200
        assert paid.json()["status"] == "PAID"

        listed = await c.get(
            f"/v1/billing/clients/{registered_client.id}/invoices", headers=admin_headers
        )
        subscription = await c.get(
            f"/v1/billing/subscriptions/{registered_client.id}", headers=admin_headers
        )

    assert len(listed.json()) == 1
    assert subscription.json()["current_period_end"] == "2026-02-01T00:00:00Z"


async def test_get_subscription_for_client_with_none_is_404(
    client, admin_headers, registered_client
):
    async with client as c:
        response = await c.get(
            f"/v1/billing/subscriptions/{registered_client.id}", headers=admin_headers
        )
    assert response.status_code == 404


async def test_suspend_reactivate_cancel_on_unknown_subscription_are_404(
    client, admin_headers, registered_client
):
    async with client as c:
        suspend = await c.post(
            f"/v1/billing/subscriptions/{registered_client.id}/suspend",
            headers=admin_headers,
            json={"reason": "test"},
        )
        reactivate = await c.post(
            f"/v1/billing/subscriptions/{registered_client.id}/reactivate", headers=admin_headers
        )
        cancel = await c.post(
            f"/v1/billing/subscriptions/{registered_client.id}/cancel", headers=admin_headers
        )
    assert suspend.status_code == 404
    assert reactivate.status_code == 404
    assert cancel.status_code == 404


async def test_cancel_subscription(client, admin_headers, registered_client):
    async with client as c:
        await c.put(
            f"/v1/billing/subscriptions/{registered_client.id}",
            headers=admin_headers,
            json={"current_period_end": "2030-01-01T00:00:00Z"},
        )
        cancelled = await c.post(
            f"/v1/billing/subscriptions/{registered_client.id}/cancel", headers=admin_headers
        )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"


async def test_create_invoice_for_unknown_client_is_404(client, admin_headers):
    async with client as c:
        response = await c.post(
            f"/v1/billing/clients/{uuid.uuid4()}/invoices",
            headers=admin_headers,
            json={
                "period_start": "2026-01-01T00:00:00Z",
                "period_end": "2026-02-01T00:00:00Z",
                "amount": "5000.00",
            },
        )
    assert response.status_code == 404


async def test_mark_paid_for_unknown_invoice_is_404(client, admin_headers):
    async with client as c:
        response = await c.post(
            f"/v1/billing/invoices/{uuid.uuid4()}/mark-paid", headers=admin_headers, json={}
        )
    assert response.status_code == 404


async def test_blocked_client_gets_402_on_a_real_request(client, db_session, monkeypatch):
    """End-to-end: with enforcement on and no subscription on file, a normal client request
    (not a billing admin endpoint) must be rejected with 402 before it does anything else.
    """
    branch = Branch(branch_code="NAIROBI-HQ", tin="A123456789Z", bhf_id="00", cmc_key="c" * 10)
    db_session.add(branch)
    unpaid_client = Client(
        name="UNPAID-TENANT",
        client_secret_hash=hash_client_secret("irrelevant"),
        allowed_branch_ids=[],
    )
    db_session.add(unpaid_client)
    await db_session.flush()

    enforced_settings = get_settings().model_copy(update={"billing_enforcement_enabled": True})
    monkeypatch.setattr("app.services.client_auth.get_settings", lambda: enforced_settings)

    async with client as c:
        response = await c.get(
            "/v1/items",
            params={"branch_code": "NAIROBI-HQ"},
            headers=bearer_header(unpaid_client),
        )

    assert response.status_code == 402
    assert "no billing subscription" in response.json()["detail"]


async def test_client_within_grace_period_is_not_blocked(client, db_session, monkeypatch):
    branch = Branch(branch_code="NAIROBI-HQ", tin="A123456789Z", bhf_id="00", cmc_key="c" * 10)
    db_session.add(branch)
    await db_session.flush()

    paying_client = Client(
        name="PAYING-TENANT",
        client_secret_hash=hash_client_secret("irrelevant"),
        allowed_branch_ids=[branch.id],
    )
    db_session.add(paying_client)
    await db_session.flush()

    await billing_service.upsert_subscription(
        db_session,
        paying_client.id,
        "STANDARD",
        datetime.now(UTC) - timedelta(days=1),  # lapsed yesterday, within default 7-day grace
        grace_period_days=None,
    )
    await db_session.flush()

    enforced_settings: Settings = get_settings().model_copy(
        update={"billing_enforcement_enabled": True, "billing_grace_period_days": 7}
    )
    monkeypatch.setattr("app.services.client_auth.get_settings", lambda: enforced_settings)

    async with client as c:
        response = await c.get(
            "/v1/items",
            params={"branch_code": "NAIROBI-HQ"},
            headers=bearer_header(paying_client),
        )

    assert response.status_code == 200
