"""API tests for POST /oauth/token — the real OAuth2 client-credentials flow end-to-end
(spec §15). Every other test in this suite bypasses this endpoint via tests/auth_helpers.py
for speed; this file is what actually exercises it.
"""

import uuid
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from jose import jwt

from app.core.config import get_settings
from app.db.models import Client, Customer
from app.db.session import get_db
from app.main import app
from app.services.client_auth import hash_client_secret


@pytest.fixture
def api_client(db_session):
    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    try:
        transport = ASGITransport(app=app)
        yield AsyncClient(transport=transport, base_url="http://test")
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
async def registered_client(db_session) -> tuple[Client, str]:
    raw_secret = "correct-horse-battery-staple"
    customer = Customer(name="SAP-TEST", allowed_branch_ids=[])
    db_session.add(customer)
    await db_session.flush()
    client = Client(
        name="SAP-TEST", client_secret_hash=hash_client_secret(raw_secret), customer_id=customer.id
    )
    db_session.add(client)
    await db_session.flush()
    return client, raw_secret


async def test_issue_token_with_valid_credentials(api_client, registered_client):
    client, raw_secret = registered_client
    async with api_client as c:
        response = await c.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": str(client.id),
                "client_secret": raw_secret,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0

    settings = get_settings()
    payload = jwt.decode(
        body["access_token"], settings.jwt_secret_key, algorithms=[settings.jwt_algorithm]
    )
    assert payload["sub"] == str(client.id)


async def test_issued_token_authenticates_a_real_request(api_client, registered_client):
    client, raw_secret = registered_client
    async with api_client as c:
        token_response = await c.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": str(client.id),
                "client_secret": raw_secret,
            },
        )
        access_token = token_response.json()["access_token"]

        codes_response = await c.get(
            "/v1/codes/tax-type", headers={"Authorization": f"Bearer {access_token}"}
        )
    assert codes_response.status_code == 200


async def test_issue_token_wrong_secret_is_401(api_client, registered_client):
    client, _ = registered_client
    async with api_client as c:
        response = await c.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": str(client.id),
                "client_secret": "wrong-secret",
            },
        )
    assert response.status_code == 401


async def test_issue_token_unknown_client_id_is_401(api_client):
    async with api_client as c:
        response = await c.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": str(uuid.uuid4()),
                "client_secret": "whatever",
            },
        )
    assert response.status_code == 401


async def test_issue_token_wrong_grant_type_is_400(api_client, registered_client):
    client, raw_secret = registered_client
    async with api_client as c:
        response = await c.post(
            "/oauth/token",
            data={
                "grant_type": "password",
                "client_id": str(client.id),
                "client_secret": raw_secret,
            },
        )
    assert response.status_code == 400


async def test_issue_token_for_revoked_client_is_401(api_client, db_session, registered_client):
    client, raw_secret = registered_client
    client.revoked_at = datetime.now(UTC)
    await db_session.flush()

    async with api_client as c:
        response = await c.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": str(client.id),
                "client_secret": raw_secret,
            },
        )
    assert response.status_code == 401


async def test_token_issued_before_revocation_stops_working_after(
    api_client, db_session, registered_client
):
    client, raw_secret = registered_client
    async with api_client as c:
        token_response = await c.post(
            "/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": str(client.id),
                "client_secret": raw_secret,
            },
        )
        access_token = token_response.json()["access_token"]

        client.revoked_at = datetime.now(UTC)
        await db_session.flush()

        codes_response = await c.get(
            "/v1/codes/tax-type", headers={"Authorization": f"Bearer {access_token}"}
        )
    assert codes_response.status_code == 401
