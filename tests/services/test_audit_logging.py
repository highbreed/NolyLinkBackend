"""Tests for AuditLogMiddleware's PII-aware redaction (spec §15): "All inbound payloads
logged (with PII/tax data access-controlled)".
"""

import logging
import uuid

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select

from app.core.audit import REDACTED_KEYS
from app.core.config import get_settings
from app.db.models import Branch, Client, ClientCallLog, Customer
from app.db.session import get_db
from app.main import app
from app.services.client_auth import hash_client_secret
from tests.auth_helpers import bearer_header


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
async def branch(db_session) -> Branch:
    b = Branch(branch_code="NAIROBI-HQ", tin="A123456789Z", bhf_id="00", cmc_key="c" * 10)
    db_session.add(b)
    await db_session.flush()
    return b


@pytest.fixture
async def auth_headers(db_session, branch) -> dict[str, str]:
    customer = Customer(name="SAP-TEST", allowed_branch_ids=[branch.id])
    db_session.add(customer)
    await db_session.flush()
    registered_client = Client(
        name="SAP-TEST",
        client_secret_hash=hash_client_secret("irrelevant"),
        customer_id=customer.id,
    )
    db_session.add(registered_client)
    await db_session.flush()
    return bearer_header(registered_client)


async def test_customer_pii_is_redacted_from_audit_log(api_client, branch, auth_headers, caplog):
    kra_base_url = get_settings().kra_base_url
    envelope = {
        "resultCd": "000",
        "resultMsg": "It is succeeded",
        "resultDt": "20200226143124",
        "data": None,
    }
    with respx.mock(base_url=kra_base_url) as mock, caplog.at_level(logging.INFO, logger="audit"):
        mock.post("/saveBhfCustomer").mock(return_value=Response(200, json=envelope))
        async with api_client as c:
            await c.post(
                "/v1/branches/customers",
                json={
                    "branch_code": "NAIROBI-HQ",
                    "cust_no": "000000001",
                    "cust_tin": "P123456789A",
                    "cust_nm": "John Customer",
                    "email": "john@example.com",
                },
                headers=auth_headers,
            )

    audit_records = [r for r in caplog.records if r.name == "audit"]
    assert len(audit_records) == 1
    body = audit_records[0].body

    assert body["cust_tin"] == "***REDACTED***"
    assert body["cust_nm"] == "***REDACTED***"
    assert body["email"] == "***REDACTED***"
    # Non-sensitive fields still come through, so the log stays useful for debugging.
    assert body["branch_code"] == "NAIROBI-HQ"
    assert body["cust_no"] == "000000001"

    # The redacted values themselves never appear anywhere in the log record.
    rendered = str(audit_records[0].__dict__)
    assert "P123456789A" not in rendered
    assert "John Customer" not in rendered
    assert "john@example.com" not in rendered


def test_cmc_key_never_appears_in_audit_log():
    """cmcKey never appears in an inbound request body (clients only ever send branch_code),
    but this asserts the redaction key list explicitly covers it as defense in depth for any
    future endpoint that might accept it directly.
    """
    assert "cmcKey" in REDACTED_KEYS


async def test_audit_log_records_method_path_and_status(api_client, caplog):
    with caplog.at_level(logging.INFO, logger="audit"):
        async with api_client as c:
            await c.get("/v1/health")

    audit_records = [r for r in caplog.records if r.name == "audit"]
    assert len(audit_records) == 1
    assert audit_records[0].method == "GET"
    assert audit_records[0].path == "/v1/health"
    assert audit_records[0].status_code == 200


async def test_client_call_log_persists_request_and_response(
    api_client, branch, auth_headers, db_session
):
    kra_base_url = get_settings().kra_base_url
    envelope = {
        "resultCd": "000",
        "resultMsg": "It is succeeded",
        "resultDt": "20200226143124",
        "data": None,
    }
    with respx.mock(base_url=kra_base_url) as mock:
        mock.post("/saveBhfCustomer").mock(return_value=Response(200, json=envelope))
        async with api_client as c:
            response = await c.post(
                "/v1/branches/customers",
                json={
                    "branch_code": "NAIROBI-HQ",
                    "cust_no": "000000001",
                    "cust_tin": "P123456789A",
                    "cust_nm": "John Customer",
                    "email": "john@example.com",
                },
                headers=auth_headers,
            )
    assert response.status_code == 201

    row = (
        await db_session.execute(
            select(ClientCallLog).where(ClientCallLog.path == "/v1/branches/customers")
        )
    ).scalar_one()

    assert row.method == "POST"
    assert row.status_code == 201
    assert row.duration_ms >= 0
    assert row.request_body["cust_tin"] == "***REDACTED***"
    assert row.request_body["cust_nm"] == "***REDACTED***"
    assert row.request_body["branch_code"] == "NAIROBI-HQ"
    # The response body is genuinely captured now — never true before this change.
    assert row.response_body == response.json()
    assert row.response_body["resultCd"] == "000"


async def test_client_call_log_persist_failure_is_swallowed_not_raised(caplog, db_session):
    """A DB failure writing client_call_log must never break the actual client-facing
    response. Exercised directly against _persist_call_log (a nonexistent client_id can
    never satisfy the FK) since every real request path always has either None or a
    FK-valid client_id — there's no way to provoke this through a full HTTP round-trip.

    db_session is requested (but otherwise unused) purely so its fixture patches
    app.db.session.AsyncSessionLocal to the test database before this calls
    _persist_call_log directly — without it this would hit real dev data.
    """
    from app.core.audit import _persist_call_log

    with caplog.at_level(logging.ERROR, logger="audit.persist"):
        await _persist_call_log(
            request_id="test-request-id",
            client_id=str(uuid.uuid4()),
            method="GET",
            path="/v1/health",
            status_code=200,
            duration_ms=1,
            request_body=None,
            response_body=None,
        )

    persist_failure_records = [r for r in caplog.records if r.name == "audit.persist"]
    assert len(persist_failure_records) == 1
    assert "Failed to persist client_call_log row" in persist_failure_records[0].message
