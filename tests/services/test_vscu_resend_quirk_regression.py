"""Regression coverage for the documented /items/saveItems and /stock/saveStockItems "local
resend" quirk (app/integrations/vscu/schemas/item.py, app/integrations/vscu/schemas/stock.py
module docstrings; confirmed again by docs/kra_specs/VSCU_v2.0.21_Local_API_Contracts.md's
"Open verification tasks" §5): the local VSCU app queues a resend on 894/896 and
unconditionally rewrites its response to resultCd="000" either way, so a success response from
either save endpoint is indistinguishable, on the wire, from a silently-queued resend that may
never land. There is no field to assert on that proves the difference — that's the point of
the quirk — so what's actually regression-testable is the *mitigation*: that our own save
paths don't claim stronger confirmation than they have, and that the reconcile endpoints built
for this (POST /v1/vscu/{branch_id}/items/{item_cd}/reconcile and
.../stock/{sar_no}/reconcile) correctly report "not confirmed" even after a bare "000" success,
by checking KRA's own trusted read-side truth (/items/selectItems, /stock/selectStockItems)
instead of trusting the save response.
"""

import uuid
from decimal import Decimal

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.config import get_settings
from app.db.models import (
    Branch,
    Client,
    Customer,
    Item,
    KraCodeList,
    KraItemClass,
    StockIOSubmission,
    VscuDeviceState,
)
from app.db.session import get_db
from app.integrations.vscu.client import VSCUClient
from app.main import app
from app.schemas.client.stock_in import ClientStockIOIn, ClientStockIOLine
from app.services.client_auth import hash_client_secret
from app.services.vscu_stock_service import reconcile_by_sar_no, submit_stock_io
from app.workers.outbox_dispatcher import _dispatch_pending
from tests.auth_helpers import bearer_header

VSCU_URL = "http://vscu-branch.test:8088"
ITEM_CLS_CD = "5020150101"


def bare_success_envelope() -> dict:
    """What /items/saveItems and /stock/saveStockItems ALWAYS return on their happy path —
    no field anywhere here distinguishes "real KRA accepted it" from "queued for resend,
    rewritten to 000 anyway" (see module docstring). That indistinguishability is the quirk.
    """
    return {
        "resultCd": "000",
        "resultMsg": "The request has been processed successfully",
        "resultDt": "20260901120000",
        "data": None,
    }


async def test_item_registration_success_is_not_proof_until_reconciled(db_session, monkeypatch):
    monkeypatch.setenv("VSCU_ENABLED", "true")
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    get_settings.cache_clear()
    try:
        branch = Branch(
            branch_code="VSCU-QUIRK",
            tin="A123456789Z",
            bhf_id="00",
            cmc_key="unused",
            device_type="VSCU",
        )
        db_session.add(branch)
        await db_session.flush()
        db_session.add(VscuDeviceState(branch_id=branch.id, base_url=VSCU_URL, dvc_srl_no="SN-1"))
        db_session.add_all(
            [
                KraCodeList(cd_cls="17", cd="NT", cd_nm="Net"),
                KraCodeList(cd_cls="10", cd="BA", cd_nm="Bag"),
                KraCodeList(cd_cls="05", cd="KE", cd_nm="Kenya"),
                KraItemClass(item_cls_cd=ITEM_CLS_CD, item_cls_nm="Bread"),
            ]
        )
        customer = Customer(name="QUIRK-TEST", allowed_branch_ids=[branch.id])
        db_session.add(customer)
        await db_session.flush()
        registered_client = Client(
            name="QUIRK-TEST",
            client_secret_hash=hash_client_secret("irrelevant"),
            customer_id=customer.id,
        )
        db_session.add(registered_client)
        await db_session.flush()
        auth_headers = bearer_header(registered_client)

        async def _override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_get_db
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                # Step 1: register the item. The local VSCU app could have silently queued a
                # resend on a 894/896 upstream failure instead of truly registering it — we
                # have no way to tell from this response alone, which is exactly the point.
                with respx.mock(base_url=VSCU_URL) as mock:
                    mock.post("/items/saveItems").mock(
                        return_value=Response(200, json=bare_success_envelope())
                    )
                    create_response = await c.post(
                        "/v1/items",
                        json={
                            "branch_code": "VSCU-QUIRK",
                            "sku": "QUIRK-SKU",
                            "name": "Bread 400g",
                            "item_classification_cd": ITEM_CLS_CD,
                            "unit_of_measure": "BA",
                            "packaging_unit": "NT",
                            "product_type": "FINISHED_PRODUCT",
                            "tax_category": "STANDARD16",
                            "unit_price": "100.00",
                        },
                        headers=auth_headers,
                    )
                assert create_response.status_code == 201
                item_cd = create_response.json()["item_cd"]

                # Step 2: reconcile against KRA's own trusted read side — simulated here as
                # NOT actually holding the item (the resend never landed). This is the
                # scenario the "000" in step 1 could not rule out.
                with respx.mock(base_url=VSCU_URL) as mock:
                    mock.post("/items/selectItems").mock(
                        return_value=Response(
                            200,
                            json={
                                "resultCd": "000",
                                "resultMsg": "ok",
                                "resultDt": "20260901120000",
                                "data": {"itemList": []},
                            },
                        )
                    )
                    reconcile_response = await c.post(
                        f"/v1/vscu/{branch.id}/items/{item_cd}/reconcile",
                        headers={"X-Admin-Api-Key": "test-admin-key"},
                    )
        finally:
            app.dependency_overrides.pop(get_db, None)

        # The whole point: a 201 from registration never guaranteed KRA actually has it —
        # reconciliation is the only way to know, and here it correctly says "no".
        assert reconcile_response.status_code == 200
        assert reconcile_response.json()["confirmed"] is False
    finally:
        get_settings.cache_clear()


@pytest.fixture
def session_factory(db_engine: AsyncEngine):
    return async_sessionmaker(bind=db_engine, expire_on_commit=False)


async def test_stock_submission_success_is_not_proof_until_reconciled(session_factory, monkeypatch):
    monkeypatch.setenv("VSCU_ENABLED", "true")
    get_settings.cache_clear()
    try:
        async with session_factory() as db:
            branch = Branch(
                branch_code=f"VSCU-STOCK-QUIRK-{uuid.uuid4().hex[:8]}",
                tin=f"A{uuid.uuid4().int % 10**9:09d}Z",
                bhf_id="00",
                cmc_key="unused",
                device_type="VSCU",
            )
            db.add(branch)
            await db.flush()
            db.add(VscuDeviceState(branch_id=branch.id, base_url=VSCU_URL, dvc_srl_no="SN-1"))
            db.add(
                Item(
                    branch_id=branch.id,
                    sku="QUIRK-STOCK-SKU",
                    item_cd="KE2NTBA0000001",
                    item_cls_cd=ITEM_CLS_CD,
                    item_nm="Bread",
                    tax_ty_cd="B",
                    dft_prc=Decimal("100.00"),
                    pkg_unit_cd="NT",
                    qty_unit_cd="BA",
                )
            )
            await db.commit()
            await db.refresh(branch)

            # Step 1: submit the stock movement (outbox-enqueued, no direct VSCU call here).
            # /stock/saveStockItems will return the same bare "000" whether KRA truly recorded
            # it or the local app queued a resend, once the outbox dispatcher sends it below.
            submission = await submit_stock_io(
                db,
                branch,
                ClientStockIOIn(
                    branch_code=branch.branch_code,
                    external_ref="12345",
                    sar_ty_cd="02",
                    lines=[ClientStockIOLine(sku="QUIRK-STOCK-SKU", quantity=5, unit_price=100)],
                ),
            )
            await db.commit()

        with respx.mock(base_url=VSCU_URL) as mock:
            mock.post("/stock/saveStockItems").mock(
                return_value=Response(200, json=bare_success_envelope())
            )
            await _dispatch_pending(session_factory)

        async with session_factory() as db:
            refreshed = await db.get(StockIOSubmission, submission.id)
            assert refreshed.status == "SENT"

        # Step 2: reconcile against KRA's own trusted read side — simulated here as NOT
        # actually holding sarNo=12345. The "SENT" status above could not rule this out.
        async with session_factory() as db:
            branch = await db.scalar(select(Branch).where(Branch.id == branch.id))
            vscu_client = VSCUClient(db=db)
            try:
                with respx.mock(base_url=VSCU_URL) as mock:
                    mock.post("/stock/selectStockItems").mock(
                        return_value=Response(
                            200,
                            json={
                                "resultCd": "000",
                                "resultMsg": "ok",
                                "resultDt": "20260901120000",
                                "data": {"stockList": []},
                            },
                        )
                    )
                    matched = await reconcile_by_sar_no(vscu_client, branch, 12345)
            finally:
                await vscu_client.aclose()

        # The whole point: "SENT" never guaranteed KRA actually has it — reconciliation is
        # the only way to know, and here it correctly finds nothing.
        assert matched is None
    finally:
        get_settings.cache_clear()
