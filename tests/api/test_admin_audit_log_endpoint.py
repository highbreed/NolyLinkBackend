"""API tests for GET /v1/admin/audit-log — real backend, previously an in-browser mock with
no server-side audit trail of admin actions at all.
"""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.db.models import AdminAuditLog
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


async def test_audit_log_requires_admin(client):
    response = await client.get("/v1/admin/audit-log")
    assert response.status_code == 403


async def test_audit_log_filters_by_target_and_free_text(client, db_session, admin_headers):
    db_session.add_all(
        [
            AdminAuditLog(
                admin_user_email="alice",
                action="customer.create",
                target_type="customer",
                target_id="c1",
                after_state={"name": "Acme"},
            ),
            AdminAuditLog(
                admin_user_email="bob",
                action="subscription.suspend",
                target_type="customer",
                target_id="c2",
                reason="chargeback",
            ),
            AdminAuditLog(
                admin_user_email=None,
                action="device.initialize",
                target_type="branch",
                target_id="b1",
            ),
        ]
    )
    await db_session.flush()

    response = await client.get(
        "/v1/admin/audit-log", params={"target_type": "customer"}, headers=admin_headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert {item["target_type"] for item in body["items"]} == {"customer"}

    response = await client.get(
        "/v1/admin/audit-log", params={"q": "alice"}, headers=admin_headers
    )
    assert [item["action"] for item in response.json()["items"]] == ["customer.create"]

    response = await client.get(
        "/v1/admin/audit-log", params={"target_id": "b1"}, headers=admin_headers
    )
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["admin_user_email"] is None


async def test_audit_log_filters_by_date_range(client, db_session, admin_headers):
    old_row = AdminAuditLog(
        admin_user_email="alice", action="old.action", target_type="customer", target_id="c1"
    )
    recent_row = AdminAuditLog(
        admin_user_email="alice", action="recent.action", target_type="customer", target_id="c1"
    )
    db_session.add_all([old_row, recent_row])
    await db_session.flush()
    old_row.created_at = datetime.now(UTC) - timedelta(days=10)
    await db_session.flush()

    response = await client.get(
        "/v1/admin/audit-log",
        params={"since": (datetime.now(UTC) - timedelta(days=1)).isoformat()},
        headers=admin_headers,
    )
    assert [item["action"] for item in response.json()["items"]] == ["recent.action"]


async def test_audit_log_paginates(client, db_session, admin_headers):
    for i in range(3):
        db_session.add(
            AdminAuditLog(
                admin_user_email="alice",
                action=f"action.{i}",
                target_type="customer",
                target_id="c1",
            )
        )
    await db_session.flush()

    response = await client.get(
        "/v1/admin/audit-log", params={"page_size": 2}, headers=admin_headers
    )
    body = response.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2
