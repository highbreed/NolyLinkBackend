"""API tests for the Phase 8 outbox-pattern endpoints (spec §18): purchases, stock,
imports, notices. Each mutating POST is checked for the 202 Accepted + outbox-row contract
(spec §14); GETs are checked against their local cache.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models import Branch, Client, Item, Outbox, PurchaseSalesCache
from app.db.session import get_db
from app.main import app
from app.services import import_item_service, notice_service, stock_service
from app.services.client_auth import hash_client_secret
from app.services.kra_adapter import KraAdapter
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
    await db_session.flush()
    return b


@pytest.fixture
async def auth_headers(db_session, branch) -> dict[str, str]:
    registered_client = Client(
        name="SAP-TEST",
        client_secret_hash=hash_client_secret("irrelevant"),
        allowed_branch_ids=[branch.id],
    )
    db_session.add(registered_client)
    await db_session.flush()
    return bearer_header(registered_client)


async def test_stock_master_returns_202_and_enqueues_outbox_row(
    client, branch, db_session, auth_headers
):
    async with client as c:
        response = await c.post(
            "/v1/stock/master",
            json={"branch_code": "NAIROBI-HQ", "sku": "BREAD", "remaining_quantity": "48"},
            headers=auth_headers,
        )
    assert response.status_code == 202

    rows = (
        (await db_session.execute(select(Outbox).where(Outbox.endpoint == "/saveStockMaster")))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].payload["itemCd"] == "KE2NTBA0000001"


async def test_stock_movements_post_and_get(client, branch, db_session, auth_headers):
    kra_base_url = get_settings().kra_base_url
    res_body = {
        "resultCd": "000",
        "resultMsg": "It is succeeded",
        "resultDt": "20200226143124",
        "data": {"stockMoveList": [{"anything": "the source spec doesn't say"}]},
    }
    with respx.mock(base_url=kra_base_url) as mock:
        mock.post("/selectStockMoveList").mock(return_value=Response(200, json=res_body))
        kra = KraAdapter(db=db_session)
        await stock_service.refresh_stock_moves(db_session, kra, branch)
        await kra.aclose()
        await db_session.commit()

    async with client as c:
        post_response = await c.post(
            "/v1/stock/movements",
            json={
                "branch_code": "NAIROBI-HQ",
                "external_ref": "SIO-0001",
                "sar_ty_cd": "02",
                "lines": [{"sku": "BREAD", "quantity": "2", "unit_price": "100.00"}],
            },
            headers=auth_headers,
        )
        assert post_response.status_code == 202
        assert post_response.json()["status"] == "PENDING"

        get_response = await c.get(
            "/v1/stock/movements", params={"branch_code": "NAIROBI-HQ"}, headers=auth_headers
        )
    assert get_response.status_code == 200
    assert len(get_response.json()) == 1


async def test_purchases_pending_and_submit(client, branch, db_session, auth_headers):
    db_session.add(
        PurchaseSalesCache(
            branch_id=branch.id,
            spplr_tin="P987654321B",
            spplr_invc_no="SUP-INV-0001",
            tot_amt="232.00",
            raw_data=load_fixture("trns_purchase_sales_res")["data"]["saleList"][0],
        )
    )
    await db_session.flush()

    async with client as c:
        pending_response = await c.get(
            "/v1/purchases/pending",
            params={"branch_code": "NAIROBI-HQ"},
            headers=auth_headers,
        )
        assert len(pending_response.json()) == 1

        submit_response = await c.post(
            "/v1/purchases",
            json={
                "branch_code": "NAIROBI-HQ",
                "external_ref": "PO-0001",
                "spplr_tin": "P987654321B",
                "spplr_invc_no": "SUP-INV-0001",
                "pchs_ty_cd": "N",
            },
            headers=auth_headers,
        )
    assert submit_response.status_code == 202
    assert submit_response.json()["status"] == "PENDING"

    outbox_rows = (
        (await db_session.execute(select(Outbox).where(Outbox.endpoint == "/insertTrnsPurchase")))
        .scalars()
        .all()
    )
    assert len(outbox_rows) == 1


async def test_imports_list_and_decision(client, branch, db_session, auth_headers):
    kra_base_url = get_settings().kra_base_url
    with respx.mock(base_url=kra_base_url) as mock:
        mock.post("/selectImportItemList").mock(
            return_value=Response(200, json=load_fixture("import_item_search_res"))
        )
        kra = KraAdapter(db=db_session)
        await import_item_service.refresh_import_items(db_session, kra, branch)
        await kra.aclose()
        await db_session.commit()

    async with client as c:
        list_response = await c.get(
            "/v1/imports", params={"branch_code": "NAIROBI-HQ"}, headers=auth_headers
        )
        assert len(list_response.json()) == 1

        decision_response = await c.post(
            "/v1/imports/T001/decision",
            json={
                "branch_code": "NAIROBI-HQ",
                "item_cls_cd": "5020150101",
                "item_cd": "KE1BGKG0000005",
                "decision_cd": "3",
            },
            headers=auth_headers,
        )
    assert decision_response.status_code == 202

    outbox_rows = (
        (await db_session.execute(select(Outbox).where(Outbox.endpoint == "/updateImportItem")))
        .scalars()
        .all()
    )
    assert len(outbox_rows) == 1
    assert outbox_rows[0].payload["imptItemsttsCd"] == "3"


async def test_notices_list(client, branch, db_session, auth_headers):
    kra_base_url = get_settings().kra_base_url
    with respx.mock(base_url=kra_base_url) as mock:
        mock.post("/selectNoticeList").mock(
            return_value=Response(200, json=load_fixture("notice_search_res"))
        )
        kra = KraAdapter(db=db_session)
        await notice_service.refresh_notices(db_session, kra, branch)
        await kra.aclose()
        await db_session.commit()

    async with client as c:
        response = await c.get(
            "/v1/notices", params={"branch_code": "NAIROBI-HQ"}, headers=auth_headers
        )
    assert response.status_code == 200
    assert response.json()[0]["title"] == "System maintenance"
