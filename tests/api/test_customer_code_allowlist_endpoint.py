"""API tests for GET/PUT /v1/customers/{customer_id}/code-allowlist (admin-only) — the
management surface for CustomerCodeAllowlist (app/db/models.py), see
app/services/code_allowlist_service.py for the enforcement side.
"""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.db.models import Customer
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
async def customer(db_session) -> Customer:
    c = Customer(name="SAP-TEST", allowed_branch_ids=[])
    db_session.add(c)
    await db_session.flush()
    return c


async def test_new_customer_has_no_allowlist_entries(client, admin_headers, customer):
    async with client as c:
        response = await c.get(f"/v1/customers/{customer.id}/code-allowlist", headers=admin_headers)
    assert response.status_code == 200
    assert response.json() == []


async def test_set_then_get_allowlist_for_classification(client, admin_headers, customer):
    async with client as c:
        put_response = await c.put(
            f"/v1/customers/{customer.id}/code-allowlist/tax-type",
            json={"codes": ["A", "B"]},
            headers=admin_headers,
        )
        get_response = await c.get(
            f"/v1/customers/{customer.id}/code-allowlist", headers=admin_headers
        )

    assert put_response.status_code == 200
    assert get_response.status_code == 200
    assert get_response.json() == [{"classification": "tax-type", "codes": ["A", "B"]}]


async def test_setting_allowlist_replaces_previous_set(client, admin_headers, customer):
    async with client as c:
        await c.put(
            f"/v1/customers/{customer.id}/code-allowlist/tax-type",
            json={"codes": ["A", "B"]},
            headers=admin_headers,
        )
        await c.put(
            f"/v1/customers/{customer.id}/code-allowlist/tax-type",
            json={"codes": ["C"]},
            headers=admin_headers,
        )
        response = await c.get(f"/v1/customers/{customer.id}/code-allowlist", headers=admin_headers)

    assert response.json() == [{"classification": "tax-type", "codes": ["C"]}]


async def test_setting_empty_codes_clears_restriction(client, admin_headers, customer):
    async with client as c:
        await c.put(
            f"/v1/customers/{customer.id}/code-allowlist/tax-type",
            json={"codes": ["A"]},
            headers=admin_headers,
        )
        await c.put(
            f"/v1/customers/{customer.id}/code-allowlist/tax-type",
            json={"codes": []},
            headers=admin_headers,
        )
        response = await c.get(f"/v1/customers/{customer.id}/code-allowlist", headers=admin_headers)

    assert response.json() == []


async def test_allowlist_for_unknown_customer_is_404(client, admin_headers):
    async with client as c:
        response = await c.get(
            f"/v1/customers/{uuid.uuid4()}/code-allowlist", headers=admin_headers
        )
    assert response.status_code == 404


async def test_allowlist_endpoints_require_admin_key(client, customer):
    async with client as c:
        response = await c.get(f"/v1/customers/{customer.id}/code-allowlist")
    assert response.status_code == 403
