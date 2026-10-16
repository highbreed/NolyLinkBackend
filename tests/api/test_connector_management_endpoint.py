"""API tests for POST /v1/customers/{customer_id}/connectors and its
revoke/rotate sub-endpoints — connector (OAuth2 Client credential) lifecycle management.
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
    c = Customer(name="SAP-CONNECTOR-TEST", allowed_branch_ids=[])
    db_session.add(c)
    await db_session.flush()
    return c


async def test_create_then_list_connector(client, admin_headers, customer):
    async with client as c:
        create_response = await c.post(
            f"/v1/customers/{customer.id}/connectors",
            json={"name": "SAP-PROD"},
            headers=admin_headers,
        )
        list_response = await c.get(
            f"/v1/customers/{customer.id}/connectors", headers=admin_headers
        )

    assert create_response.status_code == 201
    created = create_response.json()
    assert created["name"] == "SAP-PROD"
    assert created["client_secret"]  # shown once

    assert list_response.status_code == 200
    listed = list_response.json()
    assert len(listed) == 1
    assert listed[0]["client_id"] == created["client_id"]
    assert listed[0]["revoked_at"] is None
    assert "client_secret" not in listed[0]


async def test_revoke_connector_marks_it_revoked_and_is_idempotent(client, admin_headers, customer):
    async with client as c:
        created = (
            await c.post(
                f"/v1/customers/{customer.id}/connectors",
                json={"name": "SAP-PROD"},
                headers=admin_headers,
            )
        ).json()
        client_id = created["client_id"]

        first_revoke = await c.post(
            f"/v1/customers/{customer.id}/connectors/{client_id}/revoke", headers=admin_headers
        )
        second_revoke = await c.post(
            f"/v1/customers/{customer.id}/connectors/{client_id}/revoke", headers=admin_headers
        )

    assert first_revoke.status_code == 200
    assert first_revoke.json()["revoked_at"] is not None
    assert second_revoke.status_code == 200
    # Idempotent: the second call doesn't error and doesn't change the timestamp semantics.
    assert second_revoke.json()["revoked_at"] is not None


async def test_revoked_connector_cannot_get_a_new_token(client, admin_headers, customer):
    async with client as c:
        created = (
            await c.post(
                f"/v1/customers/{customer.id}/connectors",
                json={"name": "SAP-PROD"},
                headers=admin_headers,
            )
        ).json()
        client_id = created["client_id"]
        raw_secret = created["client_secret"]

        await c.post(
            f"/v1/customers/{customer.id}/connectors/{client_id}/revoke", headers=admin_headers
        )

        token_response = await c.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": raw_secret,
            },
        )

    assert token_response.status_code == 401


async def test_rotate_connector_issues_new_secret_for_same_client_id(
    client, admin_headers, customer
):
    async with client as c:
        created = (
            await c.post(
                f"/v1/customers/{customer.id}/connectors",
                json={"name": "SAP-PROD"},
                headers=admin_headers,
            )
        ).json()
        client_id = created["client_id"]
        old_secret = created["client_secret"]

        rotate_response = await c.post(
            f"/v1/customers/{customer.id}/connectors/{client_id}/rotate", headers=admin_headers
        )
        rotated = rotate_response.json()

        old_secret_token = await c.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": old_secret,
            },
        )
        new_secret_token = await c.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": rotated["client_secret"],
            },
        )

    assert rotate_response.status_code == 200
    assert rotated["client_id"] == client_id
    assert rotated["client_secret"] != old_secret
    assert old_secret_token.status_code == 401
    assert new_secret_token.status_code == 200


async def test_revoke_unknown_connector_is_404(client, admin_headers, customer):
    async with client as c:
        response = await c.post(
            f"/v1/customers/{customer.id}/connectors/{uuid.uuid4()}/revoke",
            headers=admin_headers,
        )
    assert response.status_code == 404


async def test_rotate_unknown_connector_is_404(client, admin_headers, customer):
    async with client as c:
        response = await c.post(
            f"/v1/customers/{customer.id}/connectors/{uuid.uuid4()}/rotate",
            headers=admin_headers,
        )
    assert response.status_code == 404


async def test_connector_endpoints_require_admin_key(client, customer):
    async with client as c:
        response = await c.post(
            f"/v1/customers/{customer.id}/connectors", json={"name": "SAP-PROD"}
        )
    assert response.status_code == 403
