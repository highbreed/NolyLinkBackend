"""API tests for POST/GET /v1/sales-invoices (spec §18) — the fiscal path, focused on
idempotency (spec §13) and the actual receipt fields being returned.
"""

import json
from decimal import Decimal

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import Branch, Client, IdempotencyKey, Item, KraCodeList
from app.db.session import get_db
from app.main import app
from app.services import idempotency
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
    db_session.add(
        Item(
            branch=b,
            sku="BREAD",
            item_cd="KE2NTBA0000001",
            item_cls_cd="5020150101",
            item_nm="Bread",
            tax_ty_cd="B",
            dft_prc=Decimal("100.00"),
            pkg_unit_cd="NT",
            qty_unit_cd="BA",
        )
    )
    db_session.add(KraCodeList(cd_cls="07", cd="01", cd_nm="CASH"))
    await db_session.flush()
    return b


@pytest.fixture
async def registered_client(db_session, branch) -> Client:
    client = Client(
        name="SAP-TEST",
        client_secret_hash=hash_client_secret("irrelevant"),
        allowed_branch_ids=[branch.id],
    )
    db_session.add(client)
    await db_session.flush()
    return client


@pytest.fixture
def auth_headers(registered_client: Client) -> dict[str, str]:
    return bearer_header(registered_client)


INVOICE_PAYLOAD = {
    "branch_code": "NAIROBI-HQ",
    "external_invoice_ref": "SAP-BILLDOC-001",
    "customer_pin": "P123456789A",
    "customer_name": "John Customer",
    "payment_method": "CASH",
    "invoice_date": "2020-02-26",
    "lines": [
        {
            "sku": "BREAD",
            "description": "Bread",
            "quantity": "2",
            "unit_of_measure": "BA",
            "unit_price": "100.00",
            "tax_category": "STANDARD16",
        }
    ],
}


def sales_envelope(cur_rcpt_no=1):
    return {
        "resultCd": "000",
        "resultMsg": "It is succeeded",
        "resultDt": "20200226143124",
        "data": {
            "curRcptNo": cur_rcpt_no,
            "totRcptNo": cur_rcpt_no,
            "intrlData": "1" * 26,
            "rcptSign": "2" * 16,
            "sdcDateTime": "20200226143124",
        },
    }


async def test_submit_sales_invoice_returns_receipt(api_client, branch, auth_headers):
    kra_base_url = get_settings().kra_base_url

    with respx.mock(base_url=kra_base_url) as mock:
        mock.post("/saveTrnsSalesOsdc").mock(return_value=Response(200, json=sales_envelope()))
        async with api_client as c:
            response = await c.post(
                "/v1/sales-invoices", json=INVOICE_PAYLOAD, headers=auth_headers
            )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "FISCALIZED"
    assert body["kra_cur_rcpt_no"] == 1
    assert body["kra_rcpt_sign"] == "2" * 16


async def test_resubmitting_same_external_ref_does_not_call_kra_again(
    api_client, branch, auth_headers
):
    kra_base_url = get_settings().kra_base_url

    with respx.mock(base_url=kra_base_url) as mock:
        route = mock.post("/saveTrnsSalesOsdc").mock(
            return_value=Response(200, json=sales_envelope())
        )
        async with api_client as c:
            first = await c.post("/v1/sales-invoices", json=INVOICE_PAYLOAD, headers=auth_headers)
            second = await c.post("/v1/sales-invoices", json=INVOICE_PAYLOAD, headers=auth_headers)

    assert route.call_count == 1
    assert first.json() == second.json()
    assert second.status_code == 201


async def test_get_sales_invoice_after_submission(api_client, branch, auth_headers):
    kra_base_url = get_settings().kra_base_url

    with respx.mock(base_url=kra_base_url) as mock:
        mock.post("/saveTrnsSalesOsdc").mock(return_value=Response(200, json=sales_envelope()))
        async with api_client as c:
            await c.post("/v1/sales-invoices", json=INVOICE_PAYLOAD, headers=auth_headers)
            response = await c.get(
                "/v1/sales-invoices/SAP-BILLDOC-001",
                params={"branch_code": "NAIROBI-HQ"},
                headers=auth_headers,
            )

    assert response.status_code == 200
    assert response.json()["kra_rcpt_sign"] == "2" * 16


async def test_get_unknown_external_ref_is_404(api_client, branch, auth_headers):
    async with api_client as c:
        response = await c.get(
            "/v1/sales-invoices/NOPE",
            params={"branch_code": "NAIROBI-HQ"},
            headers=auth_headers,
        )
    assert response.status_code == 404


async def test_missing_token_is_unauthorized(api_client, branch):
    async with api_client as c:
        response = await c.post("/v1/sales-invoices", json=INVOICE_PAYLOAD)
    assert response.status_code == 401


async def test_unregistered_sku_is_422_and_releases_idempotency_claim(
    api_client, branch, auth_headers, db_session
):
    payload = json.loads(json.dumps(INVOICE_PAYLOAD))
    payload["lines"][0]["sku"] = "NOT-REGISTERED"
    payload["external_invoice_ref"] = "SAP-BILLDOC-BAD"

    async with api_client as c:
        response = await c.post("/v1/sales-invoices", json=payload, headers=auth_headers)
    assert response.status_code == 422

    # The claim row should have been released, not left stuck forever.
    remaining = await db_session.scalar(
        select(IdempotencyKey).where(IdempotencyKey.external_ref == "SAP-BILLDOC-BAD")
    )
    assert remaining is None


async def test_kra_rejection_returns_502_and_releases_idempotency_claim(
    api_client, branch, auth_headers, db_session
):
    kra_base_url = get_settings().kra_base_url
    payload = json.loads(json.dumps(INVOICE_PAYLOAD))
    payload["external_invoice_ref"] = "SAP-BILLDOC-KRA-DOWN"

    with respx.mock(base_url=kra_base_url) as mock:
        mock.post("/saveTrnsSalesOsdc").mock(
            return_value=Response(
                200,
                json={
                    "resultCd": "991",
                    "resultMsg": "server error",
                    "resultDt": "20200226143124",
                    "data": None,
                },
            )
        )
        async with api_client as c:
            response = await c.post("/v1/sales-invoices", json=payload, headers=auth_headers)

    assert response.status_code == 502
    assert "991" in response.json()["detail"]

    # The claim row should have been released, not left stuck forever — a real KRA-side
    # failure must let the client retry the same external_ref, not be permanently blocked.
    remaining = await db_session.scalar(
        select(IdempotencyKey).where(IdempotencyKey.external_ref == "SAP-BILLDOC-KRA-DOWN")
    )
    assert remaining is None


async def test_concurrent_submission_of_same_external_ref_is_409(
    api_client, branch, auth_headers, registered_client, db_session
):
    payload = json.loads(json.dumps(INVOICE_PAYLOAD))
    payload["external_invoice_ref"] = "SAP-BILLDOC-RACE"

    # Simulate a first request that's already in flight (claimed but not yet completed) by
    # calling the same claim primitive the endpoint itself uses.
    claimed = await idempotency.claim_or_get_cached(
        db_session, registered_client.id, "SAP-BILLDOC-RACE", "/v1/sales-invoices"
    )
    assert claimed is None  # confirms this test really did win the claim
    await db_session.commit()

    async with api_client as c:
        response = await c.post("/v1/sales-invoices", json=payload, headers=auth_headers)

    assert response.status_code == 409
