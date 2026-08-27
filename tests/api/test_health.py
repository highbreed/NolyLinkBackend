"""API tests for GET /v1/health, including Phase 10's last-successful-poll-per-resource."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.models import KraSyncState
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


async def test_health_returns_ok_with_no_sync_state_yet(client):
    async with client as c:
        response = await c.get("/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["last_successful_poll"] == {}


async def test_health_reports_last_successful_poll_per_resource(client, db_session):
    db_session.add(
        KraSyncState(tin="A123456789Z", bhf_id="00", resource="codes", last_req_dt="20200101000000")
    )
    await db_session.flush()

    async with client as c:
        response = await c.get("/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert "codes" in body["last_successful_poll"]
