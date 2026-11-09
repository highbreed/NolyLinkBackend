"""API tests for GET /v1/admin/client-calls — the admin-gated, persisted record of every
inbound client call + response (app/db/models.py's ClientCallLog, written by
AuditLogMiddleware), distinct from admin_dashboard.py's KRA/VSCU outbound call-log viewer.
"""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.db.models import Client, ClientCallLog, Customer
from app.db.session import get_db
from app.main import app


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
    customer = Customer(name="CLIENT-CALLS-TEST", allowed_branch_ids=[])
    db_session.add(customer)
    await db_session.flush()
    c = Client(name="connector", client_secret_hash="irrelevant", customer_id=customer.id)
    db_session.add(c)
    await db_session.flush()
    return c


async def test_list_client_calls_requires_admin(client):
    response = await client.get("/v1/admin/client-calls")
    assert response.status_code == 403


async def test_list_client_calls_paginates_and_filters(
    client, db_session, admin_headers, registered_client
):
    base = datetime.now(UTC)
    for i in range(3):
        db_session.add(
            ClientCallLog(
                request_id=f"req-{i}",
                client_id=registered_client.id,
                method="POST",
                path="/v1/sales-invoices",
                status_code=201,
                duration_ms=10 + i,
                request_body={"external_invoice_ref": f"INV-{i}"},
                response_body={"status": "FISCALIZED"},
                created_at=base + timedelta(seconds=i),
            )
        )
    db_session.add(
        ClientCallLog(
            request_id="req-health",
            client_id=None,
            method="GET",
            path="/v1/health",
            status_code=200,
            duration_ms=1,
        )
    )
    await db_session.flush()

    response = await client.get(
        "/v1/admin/client-calls",
        params={"path": "/v1/sales-invoices", "page_size": 2},
        headers=admin_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert body["page"] == 1
    assert body["page_size"] == 2
    assert len(body["items"]) == 2
    # Newest first.
    assert body["items"][0]["request_id"] == "req-2"
    assert body["items"][0]["response_body"] == {"status": "FISCALIZED"}

    response_page_2 = await client.get(
        "/v1/admin/client-calls",
        params={"path": "/v1/sales-invoices", "page_size": 2, "page": 2},
        headers=admin_headers,
    )
    assert [item["request_id"] for item in response_page_2.json()["items"]] == ["req-0"]


async def test_list_client_calls_filters_by_client_id(
    client, db_session, admin_headers, registered_client
):
    other_customer = Customer(name="OTHER-CLIENT-CALLS-TEST", allowed_branch_ids=[])
    db_session.add(other_customer)
    await db_session.flush()
    other_client = Client(
        name="other-connector", client_secret_hash="irrelevant", customer_id=other_customer.id
    )
    db_session.add(other_client)
    await db_session.flush()

    db_session.add(
        ClientCallLog(
            request_id="mine",
            client_id=registered_client.id,
            method="GET",
            path="/v1/health",
            status_code=200,
            duration_ms=1,
        )
    )
    db_session.add(
        ClientCallLog(
            request_id="someone-elses",
            client_id=other_client.id,
            method="GET",
            path="/v1/health",
            status_code=200,
            duration_ms=1,
        )
    )
    await db_session.flush()

    response = await client.get(
        "/v1/admin/client-calls",
        params={"client_id": str(registered_client.id)},
        headers=admin_headers,
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["request_id"] for item in items] == ["mine"]
