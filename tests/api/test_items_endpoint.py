"""API tests for POST/GET /v1/items and POST /v1/items/{sku}/composition (spec §18)."""

import json

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from app.core.config import get_settings
from app.db.models import Branch, KraCodeList
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
async def branch(db_session) -> Branch:
    b = Branch(branch_code="NAIROBI-HQ", tin="A123456789Z", bhf_id="00", cmc_key="c" * 10)
    db_session.add(b)
    db_session.add_all(
        [
            KraCodeList(cd_cls="17", cd="NT", cd_nm="Net"),
            KraCodeList(cd_cls="10", cd="BA", cd_nm="Bag"),
        ]
    )
    await db_session.flush()
    return b


def success_envelope(data=None):
    return {
        "resultCd": "000",
        "resultMsg": "It is succeeded",
        "resultDt": "20200226143124",
        "data": data,
    }


ITEM_PAYLOAD = {
    "branch_code": "NAIROBI-HQ",
    "sku": "SKU-001",
    "name": "Bread 400g",
    "item_classification_cd": "5020150101",
    "unit_of_measure": "BA",
    "packaging_unit": "NT",
    "product_type": "FINISHED_PRODUCT",
    "tax_category": "STANDARD16",
    "unit_price": "100.00",
}


async def test_create_item_generates_item_cd_and_persists(client, branch):
    kra_base_url = get_settings().kra_base_url
    with respx.mock(base_url=kra_base_url) as mock:
        mock.post("/saveItem").mock(return_value=Response(200, json=success_envelope()))
        async with client as c:
            response = await c.post("/v1/items", json=ITEM_PAYLOAD)

    assert response.status_code == 201
    body = response.json()
    assert body["item_cd"] == "KE2NTBA0000001"
    assert body["sku"] == "SKU-001"
    assert body["branch_code"] == "NAIROBI-HQ"


async def test_get_items_lists_created_item(client, branch):
    kra_base_url = get_settings().kra_base_url
    with respx.mock(base_url=kra_base_url) as mock:
        mock.post("/saveItem").mock(return_value=Response(200, json=success_envelope()))
        async with client as c:
            await c.post("/v1/items", json=ITEM_PAYLOAD)
            response = await c.get("/v1/items", params={"branch_code": "NAIROBI-HQ"})

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["sku"] == "SKU-001"


async def test_create_item_twice_reuses_item_cd(client, branch):
    kra_base_url = get_settings().kra_base_url
    with respx.mock(base_url=kra_base_url) as mock:
        mock.post("/saveItem").mock(return_value=Response(200, json=success_envelope()))
        async with client as c:
            first = await c.post("/v1/items", json=ITEM_PAYLOAD)
            second = await c.post("/v1/items", json={**ITEM_PAYLOAD, "unit_price": "110.00"})

    assert first.json()["item_cd"] == second.json()["item_cd"]
    assert second.json()["unit_price"] == "110.00"


async def test_item_composition_uses_stored_item_cd(client, branch):
    kra_base_url = get_settings().kra_base_url
    with respx.mock(base_url=kra_base_url) as mock:
        mock.post("/saveItem").mock(return_value=Response(200, json=success_envelope()))
        composition_route = mock.post("/saveItemComposition").mock(
            return_value=Response(200, json=success_envelope())
        )
        async with client as c:
            await c.post("/v1/items", json=ITEM_PAYLOAD)
            response = await c.post(
                "/v1/items/SKU-001/composition",
                json={"branch_code": "NAIROBI-HQ", "composition": {"bomItemList": []}},
            )

    assert response.status_code == 202
    sent_body = composition_route.calls[0].request.content
    assert json.loads(sent_body)["itemCd"] == "KE2NTBA0000001"


async def test_item_composition_unknown_sku_is_404(client, branch):
    async with client as c:
        response = await c.post(
            "/v1/items/NOT-A-SKU/composition",
            json={"branch_code": "NAIROBI-HQ", "composition": {}},
        )
    assert response.status_code == 404
