"""API tests for GET /v1/codes/{classification} (spec §18) — served from cache only."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.models import Client, KraCodeList
from app.db.session import get_db
from app.main import app
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
async def auth_headers(db_session) -> dict[str, str]:
    registered_client = Client(
        name="SAP-TEST", client_secret_hash=hash_client_secret("irrelevant"), allowed_branch_ids=[]
    )
    db_session.add(registered_client)
    await db_session.flush()
    return bearer_header(registered_client)


async def test_get_codes_by_slug(client, db_session, auth_headers):
    db_session.add(KraCodeList(cd_cls="04", cd="B", cd_nm="16%", srt_ord=2, use_yn="Y"))
    await db_session.flush()

    async with client as c:
        response = await c.get("/v1/codes/tax-type", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["cd"] == "B"
    assert body[0]["cd_nm"] == "16%"


async def test_get_codes_by_raw_cd_cls(client, db_session, auth_headers):
    db_session.add(KraCodeList(cd_cls="99", cd="X", cd_nm="Something", srt_ord=1, use_yn="Y"))
    await db_session.flush()

    async with client as c:
        response = await c.get("/v1/codes/99", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()[0]["cd"] == "X"


async def test_get_codes_unknown_classification_returns_empty_list(client, auth_headers):
    async with client as c:
        response = await c.get("/v1/codes/nonexistent", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == []


async def test_get_codes_without_token_is_401(client):
    async with client as c:
        response = await c.get("/v1/codes/tax-type")

    assert response.status_code == 401
