"""API tests confirming POST/GET /v1/sales-invoices — the SAME client-facing endpoint
tests/api/test_sales_endpoint.py exercises for OSCU — works identically for a VSCU branch,
dispatching by Branch.device_type (app/api/v1/endpoints/sales.py)."""

from decimal import Decimal

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from app.core.config import get_settings
from app.db.models import Branch, Client, Customer, Item, KraCodeList, VscuDeviceState
from app.db.session import get_db
from app.main import app
from app.services.client_auth import hash_client_secret
from tests.auth_helpers import bearer_header

VSCU_URL = "http://vscu-branch.test:8088"


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
    b = Branch(
        branch_code="VSCU-NAIROBI",
        tin="A123456789Z",
        bhf_id="00",
        cmc_key="unused",
        device_type="VSCU",
    )
    db_session.add(b)
    await db_session.flush()
    db_session.add(VscuDeviceState(branch_id=b.id, base_url=VSCU_URL, dvc_srl_no="SN-1"))
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
    customer = Customer(name="SAP-TEST", allowed_branch_ids=[branch.id])
    db_session.add(customer)
    await db_session.flush()
    client = Client(
        name="SAP-TEST",
        client_secret_hash=hash_client_secret("irrelevant"),
        customer_id=customer.id,
    )
    db_session.add(client)
    await db_session.flush()
    return client


@pytest.fixture
def auth_headers(registered_client: Client) -> dict[str, str]:
    return bearer_header(registered_client)


INVOICE_PAYLOAD = {
    "branch_code": "VSCU-NAIROBI",
    "external_invoice_ref": "SAP-BILLDOC-VSCU-001",
    "customer_pin": "P123456789A",
    "customer_name": "John Customer",
    "payment_method": "CASH",
    "invoice_date": "2026-09-01",
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


def vscu_sales_envelope(rcpt_no=1):
    return {
        "resultCd": "000",
        "resultMsg": "The request has been processed successfully",
        "resultDt": "20260901120000",
        "data": {
            "rcptNo": rcpt_no,
            "totRcptNo": rcpt_no,
            "intrlData": "1" * 26,
            "rcptSign": "2" * 16,
            "vsdcRcptPbctDate": "20260901120000",
            "sdcId": "SDC001",
            "mrcNo": "MRC001",
        },
    }


async def test_submit_sales_invoice_for_vscu_branch_returns_receipt(
    api_client, branch, auth_headers, monkeypatch
):
    monkeypatch.setenv("VSCU_ENABLED", "true")
    get_settings.cache_clear()
    try:
        with respx.mock:
            respx.post(f"{VSCU_URL}/trnsSales/saveSales").mock(
                return_value=Response(200, json=vscu_sales_envelope())
            )
            async with api_client as c:
                response = await c.post(
                    "/v1/sales-invoices", json=INVOICE_PAYLOAD, headers=auth_headers
                )

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "FISCALIZED"
        assert body["kra_cur_rcpt_no"] == 1
        assert body["kra_rcpt_sign"] == "2222-2222-2222-2222"
        assert body["kra_intrl_data"] == "1111-1111-1111-1111-1111-1111-11"
        assert body["kra_sdc_id"] == "SDC001"
        assert body["kra_cu_invoice_no"] == "SDC001/1"
        assert body["qrcode_link"] == (
            "https://etims-sbx.kra.go.ke/common/link/etims/receipt/"
            "indexEtimsReceiptData?Data=A123456789Z00" + "2" * 16
        )
        assert body["scu_information"] == {
            "date": "01/09/2026 12:00:00",
            "control_unit_number": "SDC001",
            "invoice_number": "SDC001/1",
            "internal_data": "1111-1111-1111-1111-1111-1111-11",
            "receipt_signature": "2222-2222-2222-2222",
            "receipt_number": 1,
        }
    finally:
        get_settings.cache_clear()


async def test_resubmitting_same_external_ref_does_not_call_vscu_again(
    api_client, branch, auth_headers, monkeypatch
):
    monkeypatch.setenv("VSCU_ENABLED", "true")
    get_settings.cache_clear()
    try:
        with respx.mock:
            route = respx.post(f"{VSCU_URL}/trnsSales/saveSales").mock(
                return_value=Response(200, json=vscu_sales_envelope())
            )
            async with api_client as c:
                first = await c.post(
                    "/v1/sales-invoices", json=INVOICE_PAYLOAD, headers=auth_headers
                )
                second = await c.post(
                    "/v1/sales-invoices", json=INVOICE_PAYLOAD, headers=auth_headers
                )

        assert route.call_count == 1
        assert first.json() == second.json()
    finally:
        get_settings.cache_clear()


async def test_get_sales_invoice_after_vscu_fiscalization(
    api_client, branch, auth_headers, monkeypatch
):
    monkeypatch.setenv("VSCU_ENABLED", "true")
    get_settings.cache_clear()
    try:
        with respx.mock:
            respx.post(f"{VSCU_URL}/trnsSales/saveSales").mock(
                return_value=Response(200, json=vscu_sales_envelope())
            )
            async with api_client as c:
                await c.post("/v1/sales-invoices", json=INVOICE_PAYLOAD, headers=auth_headers)
                get_response = await c.get(
                    "/v1/sales-invoices/SAP-BILLDOC-VSCU-001",
                    params={"branch_code": "VSCU-NAIROBI"},
                    headers=auth_headers,
                )
        assert get_response.status_code == 200
        assert get_response.json()["kra_rcpt_sign"] == "2222-2222-2222-2222"
    finally:
        get_settings.cache_clear()


async def test_submit_credit_note_for_vscu_branch_references_original_sale(
    api_client, branch, auth_headers, db_session, monkeypatch
):
    """POST /v1/sales-invoices/credit-notes — the same endpoint family, dispatching by
    device_type exactly like the normal-sale POST does.
    """
    monkeypatch.setenv("VSCU_ENABLED", "true")
    get_settings.cache_clear()
    try:
        db_session.add(KraCodeList(cd_cls="32", cd="06", cd_nm="Refund"))
        await db_session.flush()

        with respx.mock:
            respx.post(f"{VSCU_URL}/trnsSales/saveSales").mock(
                return_value=Response(200, json=vscu_sales_envelope())
            )
            async with api_client as c:
                sale_response = await c.post(
                    "/v1/sales-invoices", json=INVOICE_PAYLOAD, headers=auth_headers
                )
                assert sale_response.status_code == 201

                credit_note_payload = {
                    "branch_code": "VSCU-NAIROBI",
                    "external_invoice_ref": "CN-001",
                    "original_invoice_ref": "SAP-BILLDOC-VSCU-001",
                    "reason": "Refund",
                    "customer_pin": "P123456789A",
                    "customer_name": "John Customer",
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
                cn_response = await c.post(
                    "/v1/sales-invoices/credit-notes",
                    json=credit_note_payload,
                    headers=auth_headers,
                )

        assert cn_response.status_code == 201
        body = cn_response.json()
        assert body["transaction_kind"] == "CREDIT_NOTE"
        assert body["original_ref"] == "SAP-BILLDOC-VSCU-001"
        assert body["status"] == "FISCALIZED"
    finally:
        get_settings.cache_clear()
