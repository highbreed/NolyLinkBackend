"""API tests confirming POST/GET /v1/items — the SAME client-facing endpoint
tests/api/test_items_endpoint.py exercises for OSCU — works identically for a VSCU branch,
dispatching by Branch.device_type (app/api/v1/endpoints/items.py), via
app/services/mapping/vscu_item_mapper.py."""

import json
from decimal import Decimal

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from app.core.config import get_settings
from app.db.models import (
    Branch,
    Client,
    Customer,
    CustomerCodeAllowlist,
    Item,
    KraCodeList,
    KraItemClass,
    VscuDeviceState,
)
from app.db.session import get_db
from app.main import app
from app.services.client_auth import hash_client_secret
from tests.auth_helpers import bearer_header

VSCU_URL = "http://vscu-branch.test:8088"
ITEM_CLS_CD = "5020150101"


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
    db_session.add_all(
        [
            KraCodeList(cd_cls="17", cd="NT", cd_nm="Net"),
            KraCodeList(cd_cls="10", cd="BA", cd_nm="Bag"),
            KraCodeList(cd_cls="05", cd="KE", cd_nm="Kenya"),
            KraItemClass(item_cls_cd=ITEM_CLS_CD, item_cls_nm="Bread"),
        ]
    )
    await db_session.flush()
    return b


@pytest.fixture
async def auth_headers(db_session, branch) -> dict[str, str]:
    customer = Customer(name="SAP-TEST-VSCU", allowed_branch_ids=[branch.id])
    db_session.add(customer)
    await db_session.flush()
    registered_client = Client(
        name="SAP-TEST-VSCU",
        client_secret_hash=hash_client_secret("irrelevant"),
        customer_id=customer.id,
    )
    db_session.add(registered_client)
    await db_session.flush()
    return bearer_header(registered_client)


def vscu_success_envelope():
    return {
        "resultCd": "000",
        "resultMsg": "The request has been processed successfully",
        "resultDt": "20260901120000",
        "data": None,
    }


ITEM_PAYLOAD = {
    "branch_code": "VSCU-NAIROBI",
    "sku": "SKU-VSCU-001",
    "name": "Bread 400g",
    "item_classification_cd": ITEM_CLS_CD,
    "unit_of_measure": "BA",
    "packaging_unit": "NT",
    "product_type": "FINISHED_PRODUCT",
    "tax_category": "STANDARD16",
    "unit_price": "100.00",
}


async def test_create_item_for_vscu_branch_registers_and_persists(
    client, branch, auth_headers, monkeypatch
):
    monkeypatch.setenv("VSCU_ENABLED", "true")
    get_settings.cache_clear()
    try:
        with respx.mock(base_url=VSCU_URL) as mock:
            mock.post("/items/saveItems").mock(
                return_value=Response(200, json=vscu_success_envelope())
            )
            async with client as c:
                response = await c.post("/v1/items", json=ITEM_PAYLOAD, headers=auth_headers)

        assert response.status_code == 201
        body = response.json()
        assert body["item_cd"] == "KE2NTBA0000001"
        assert body["sku"] == "SKU-VSCU-001"
        assert body["branch_code"] == "VSCU-NAIROBI"
        assert body["item_cls_cd"] == ITEM_CLS_CD
    finally:
        get_settings.cache_clear()


async def test_create_item_for_vscu_branch_twice_reuses_item_cd(
    client, branch, auth_headers, monkeypatch
):
    monkeypatch.setenv("VSCU_ENABLED", "true")
    get_settings.cache_clear()
    try:
        with respx.mock(base_url=VSCU_URL) as mock:
            mock.post("/items/saveItems").mock(
                return_value=Response(200, json=vscu_success_envelope())
            )
            async with client as c:
                first = await c.post("/v1/items", json=ITEM_PAYLOAD, headers=auth_headers)
                second = await c.post(
                    "/v1/items",
                    json={**ITEM_PAYLOAD, "unit_price": "110.00"},
                    headers=auth_headers,
                )

        assert first.json()["item_cd"] == second.json()["item_cd"]
        assert second.json()["unit_price"] == "110.00"
    finally:
        get_settings.cache_clear()


async def test_create_item_for_vscu_branch_rejected_by_customer_code_allowlist(
    client, db_session, branch, monkeypatch
):
    monkeypatch.setenv("VSCU_ENABLED", "true")
    get_settings.cache_clear()
    try:
        restricted_customer = Customer(name="SAP-VSCU-RESTRICTED", allowed_branch_ids=[branch.id])
        db_session.add(restricted_customer)
        await db_session.flush()
        db_session.add(
            CustomerCodeAllowlist(
                customer_id=restricted_customer.id, classification="packaging-unit", code="CT"
            )
        )
        restricted_client = Client(
            name="SAP-VSCU-RESTRICTED",
            client_secret_hash=hash_client_secret("irrelevant"),
            customer_id=restricted_customer.id,
        )
        db_session.add(restricted_client)
        await db_session.flush()

        async with client as c:
            response = await c.post(
                "/v1/items", json=ITEM_PAYLOAD, headers=bearer_header(restricted_client)
            )
        assert response.status_code == 422
    finally:
        get_settings.cache_clear()


async def test_create_item_for_vscu_branch_kra_error_is_502(
    client, branch, auth_headers, monkeypatch
):
    monkeypatch.setenv("VSCU_ENABLED", "true")
    get_settings.cache_clear()
    try:
        with respx.mock(base_url=VSCU_URL) as mock:
            mock.post("/items/saveItems").mock(
                return_value=Response(
                    200,
                    json={
                        "resultCd": "899",
                        "resultMsg": "An error regarding Client occurred.",
                        "resultDt": None,
                        "data": None,
                    },
                )
            )
            async with client as c:
                response = await c.post("/v1/items", json=ITEM_PAYLOAD, headers=auth_headers)

        assert response.status_code == 502
    finally:
        get_settings.cache_clear()


@pytest.fixture
async def registered_item(db_session, branch) -> Item:
    item = Item(
        branch_id=branch.id,
        sku="SKU-VSCU-001",
        item_cd="KE2NTBA0000001",
        item_cls_cd=ITEM_CLS_CD,
        item_nm="Bread 400g",
        tax_ty_cd="B",
        dft_prc=Decimal("100.00"),
        pkg_unit_cd="NT",
        qty_unit_cd="BA",
    )
    db_session.add(item)
    await db_session.flush()
    return item


async def test_save_item_composition_for_vscu_branch_succeeds(
    client, branch, registered_item, auth_headers, monkeypatch
):
    monkeypatch.setenv("VSCU_ENABLED", "true")
    get_settings.cache_clear()
    try:
        with respx.mock(base_url=VSCU_URL) as mock:
            composition_route = mock.post("/items/saveItemComposition").mock(
                return_value=Response(200, json=vscu_success_envelope())
            )
            async with client as c:
                response = await c.post(
                    "/v1/items/SKU-VSCU-001/composition",
                    json={
                        "branch_code": "VSCU-NAIROBI",
                        "composition": {"cpstItemCd": "KE2NTBA0000099", "cpstQty": "3"},
                    },
                    headers=auth_headers,
                )

        assert response.status_code == 202
        sent_body = composition_route.calls[0].request.content
        assert json.loads(sent_body)["cpstItemCd"] == "KE2NTBA0000099"
        assert json.loads(sent_body)["itemCd"] == "KE2NTBA0000001"
    finally:
        get_settings.cache_clear()


async def test_save_item_composition_for_vscu_branch_missing_field_is_422(
    client, branch, registered_item, auth_headers, monkeypatch
):
    monkeypatch.setenv("VSCU_ENABLED", "true")
    get_settings.cache_clear()
    try:
        async with client as c:
            response = await c.post(
                "/v1/items/SKU-VSCU-001/composition",
                json={
                    "branch_code": "VSCU-NAIROBI",
                    "composition": {"cpstItemCd": "KE2NTBA0000099"},
                },
                headers=auth_headers,
            )
        assert response.status_code == 422
    finally:
        get_settings.cache_clear()
