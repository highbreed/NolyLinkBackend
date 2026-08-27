"""Tests for request-ID correlation (tech baseline: "correlation/request IDs on every
request") — a client-supplied X-Request-ID is honored, one is generated if absent, it's
echoed back on the response, and every log line during that request carries it.
"""

import logging
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

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


async def test_request_id_is_generated_when_absent(client):
    async with client as c:
        response = await c.get("/v1/health")

    assert "X-Request-ID" in response.headers
    # Must actually be a valid UUID, not a placeholder.
    uuid.UUID(response.headers["X-Request-ID"])


async def test_client_supplied_request_id_is_honored(client):
    my_id = str(uuid.uuid4())
    async with client as c:
        response = await c.get("/v1/health", headers={"X-Request-ID": my_id})

    assert response.headers["X-Request-ID"] == my_id


async def test_log_lines_during_the_request_carry_its_request_id(client, caplog):
    my_id = str(uuid.uuid4())
    with caplog.at_level(logging.INFO, logger="audit"):
        async with client as c:
            await c.get("/v1/health", headers={"X-Request-ID": my_id})

    audit_records = [r for r in caplog.records if r.name == "audit"]
    assert len(audit_records) == 1
    assert audit_records[0].request_id == my_id


async def test_different_requests_get_different_request_ids(client):
    async with client as c:
        first = await c.get("/v1/health")
        second = await c.get("/v1/health")

    assert first.headers["X-Request-ID"] != second.headers["X-Request-ID"]
