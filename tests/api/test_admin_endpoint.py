"""API tests for /admin/login and /admin/users — the backend portal's human login, and proof
that it grants exactly the same access as the static X-Admin-Api-Key on other admin routes
(e.g. /v1/devices/initialize), not a separate/lesser privilege tier.
"""

import json
import uuid
from pathlib import Path

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from app.core.config import get_settings
from app.db.models import Client, Customer
from app.db.session import get_db
from app.main import app
from app.services import admin_user_auth
from app.services.client_auth import hash_client_secret
from tests.auth_helpers import bearer_header

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "kra_samples"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text())


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
def admin_key_headers() -> dict[str, str]:
    admin_key = get_settings().admin_api_key
    assert admin_key, "ADMIN_API_KEY must be configured for this test run"
    return {"X-Admin-Api-Key": admin_key}


async def test_login_with_correct_credentials_returns_a_token(client, db_session):
    await admin_user_auth.create_admin_user(
        db_session, "amina", "correct horse battery", "Amina K."
    )
    await db_session.commit()

    async with client as c:
        response = await c.post(
            "/admin/login", json={"username": "amina", "password": "correct horse battery"}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]


async def test_login_with_wrong_password_is_401(client, db_session):
    await admin_user_auth.create_admin_user(db_session, "amina", "correct horse battery", None)
    await db_session.commit()

    async with client as c:
        response = await c.post("/admin/login", json={"username": "amina", "password": "wrong"})

    assert response.status_code == 401


async def test_login_with_unknown_username_is_401(client):
    async with client as c:
        response = await c.post("/admin/login", json={"username": "nobody", "password": "x"})
    assert response.status_code == 401


async def test_admin_users_endpoints_require_admin_key(client):
    async with client as c:
        response = await c.get("/admin/users")
    assert response.status_code == 403


async def test_create_user_with_admin_key_then_list(client, admin_key_headers):
    async with client as c:
        create = await c.post(
            "/admin/users",
            headers=admin_key_headers,
            json={
                "username": "brian",
                "password": "another-strong-password",
                "full_name": "Brian M.",
            },
        )
        assert create.status_code == 201
        assert create.json()["username"] == "brian"
        assert "password" not in create.json()
        assert "password_hash" not in create.json()

        listed = await c.get("/admin/users", headers=admin_key_headers)

    assert listed.status_code == 200
    assert any(u["username"] == "brian" for u in listed.json())


async def test_duplicate_username_via_api_is_409(client, admin_key_headers):
    async with client as c:
        await c.post(
            "/admin/users",
            headers=admin_key_headers,
            json={"username": "amina", "password": "first-password"},
        )
        second = await c.post(
            "/admin/users",
            headers=admin_key_headers,
            json={"username": "amina", "password": "second-password"},
        )
    assert second.status_code == 409


async def test_a_newly_logged_in_admin_can_call_device_init_like_the_static_key_can(
    client, db_session
):
    """The whole point: a portal login must grant identical admin access to other admin
    routes, not just to /admin/* itself.
    """
    await admin_user_auth.create_admin_user(db_session, "amina", "correct horse battery", None)
    await db_session.commit()

    kra_base_url = get_settings().kra_base_url
    res_body = load_fixture("device_verification_res")

    with respx.mock(base_url=kra_base_url) as mock:
        mock.post("/selectInitOsdcInfo").mock(return_value=Response(200, json=res_body))
        async with client as c:
            login = await c.post(
                "/admin/login", json={"username": "amina", "password": "correct horse battery"}
            )
            token = login.json()["access_token"]

            response = await c.post(
                "/v1/devices/initialize",
                headers={"Authorization": f"Bearer {token}"},
                json={"branch_code": "TEST-BRANCH", "tin": "A123456789Z", "bhf_id": "00"},
            )

    assert response.status_code == 201


async def test_deactivate_then_reactivate_admin_user(client, admin_key_headers, db_session):
    user = await admin_user_auth.create_admin_user(
        db_session, "amina", "correct horse battery", None
    )
    await db_session.commit()
    # Captured once, up front: the failed login attempt below rolls back the shared session,
    # which (correctly) leaves any still-held ORM object expired — re-reading user.id after
    # that would need an implicit lazy-load outside an awaited context and blow up with
    # SQLAlchemy's MissingGreenlet. A plain uuid.UUID has no such lifecycle.
    user_id = user.id

    async with client as c:
        deactivated = await c.post(f"/admin/users/{user_id}/deactivate", headers=admin_key_headers)
        assert deactivated.status_code == 200
        assert deactivated.json()["is_active"] is False

        # A deactivated user can no longer log in.
        login_attempt = await c.post(
            "/admin/login", json={"username": "amina", "password": "correct horse battery"}
        )
        assert login_attempt.status_code == 401

        reactivated = await c.post(f"/admin/users/{user_id}/reactivate", headers=admin_key_headers)
        assert reactivated.status_code == 200
        assert reactivated.json()["is_active"] is True

        login_again = await c.post(
            "/admin/login", json={"username": "amina", "password": "correct horse battery"}
        )
    assert login_again.status_code == 200


async def test_deactivate_unknown_user_is_404(client, admin_key_headers):
    async with client as c:
        response = await c.post(
            f"/admin/users/{uuid.uuid4()}/deactivate", headers=admin_key_headers
        )
    assert response.status_code == 404


async def test_reactivate_unknown_user_is_404(client, admin_key_headers):
    async with client as c:
        response = await c.post(
            f"/admin/users/{uuid.uuid4()}/reactivate", headers=admin_key_headers
        )
    assert response.status_code == 404


async def test_password_over_72_bytes_is_rejected(client, admin_key_headers):
    async with client as c:
        response = await c.post(
            "/admin/users",
            headers=admin_key_headers,
            json={"username": "amina", "password": "x" * 73},
        )
    assert response.status_code == 422


async def test_client_bearer_token_is_rejected_on_admin_routes(client, db_session):
    """A SAP/ERP client's own OAuth2 token (a valid JWT, just the wrong kind) must never work
    as admin auth — proves the "type": "admin" claim check in admin_user_auth, not just that
    an empty/garbage token is rejected.
    """
    customer = Customer(name="SOME-SAP-CLIENT", allowed_branch_ids=[])
    db_session.add(customer)
    await db_session.flush()
    a_client = Client(
        name="SOME-SAP-CLIENT",
        client_secret_hash=hash_client_secret("x"),
        customer_id=customer.id,
    )
    db_session.add(a_client)
    await db_session.flush()

    async with client as c:
        response = await c.get("/admin/users", headers=bearer_header(a_client))
    assert response.status_code == 403
