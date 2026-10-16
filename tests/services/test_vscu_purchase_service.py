"""Tests for app/services/vscu_purchase_service.py: sync into the shared PurchaseSalesCache
table, and outbox-enqueued confirmation with VSCU's own field shapes (int invcNo, the E-bucket
default, and the numeric-external_ref requirement VSCU's strict typing forces).
"""

from decimal import Decimal

import pytest
import respx
from httpx import Response
from sqlalchemy import select

from app.core.config import Settings
from app.db.models import Branch, KraSyncState, Outbox, PurchaseSalesCache, VscuDeviceState
from app.integrations.vscu.client import VSCUClient
from app.schemas.client.purchase_in import ClientPurchaseIn
from app.services.vscu_purchase_service import (
    VSCU_SAVE_PURCHASE_ENDPOINT,
    InvalidExternalRef,
    PendingPurchaseNotFound,
    submit_purchase,
    sync_purchase_sales,
)

VSCU_URL = "http://vscu-branch.test:8088"


@pytest.fixture
def vscu_client(db_session) -> VSCUClient:
    return VSCUClient(db=db_session, settings=Settings(vscu_enabled=True))


@pytest.fixture
async def branch(db_session) -> Branch:
    b = Branch(
        branch_code="VSCU-PURCH",
        tin="A123456789Z",
        bhf_id="00",
        cmc_key="unused",
        device_type="VSCU",
    )
    db_session.add(b)
    await db_session.flush()
    db_session.add(VscuDeviceState(branch_id=b.id, base_url=VSCU_URL, dvc_srl_no="SN-1"))
    await db_session.flush()
    return b


async def test_sync_purchase_sales_upserts_shared_cache(
    db_session, branch: Branch, vscu_client: VSCUClient
):
    with respx.mock:
        respx.post(f"{VSCU_URL}/trnsPurchase/selectTrnsPurchaseSales").mock(
            return_value=Response(
                200,
                json={
                    "resultCd": "000",
                    "resultMsg": "ok",
                    "resultDt": "20260901120000",
                    "data": {
                        "saleList": [
                            {
                                "spplrTin": "A987654321Z",
                                "spplrBhfId": "00",
                                "spplrInvcNo": 42,
                                "totAmt": "116.00",
                                "itemList": [],
                            }
                        ]
                    },
                },
            )
        )
        await sync_purchase_sales(db_session, vscu_client, branch)

    row = await db_session.scalar(
        select(PurchaseSalesCache).where(
            PurchaseSalesCache.branch_id == branch.id, PurchaseSalesCache.spplr_invc_no == "42"
        )
    )
    assert row is not None
    assert row.tot_amt == Decimal("116.00")

    sync_state = await db_session.scalar(
        select(KraSyncState).where(
            KraSyncState.tin == branch.tin,
            KraSyncState.bhf_id == branch.bhf_id,
            KraSyncState.resource == "purchase_sales",
        )
    )
    assert sync_state is not None


async def test_submit_purchase_without_cache_raises_not_found(db_session, branch: Branch):
    payload = ClientPurchaseIn(
        branch_code="VSCU-PURCH",
        external_ref="1001",
        spplr_tin="A987654321Z",
        spplr_invc_no="42",
        pchs_ty_cd="N",
    )
    with pytest.raises(PendingPurchaseNotFound):
        await submit_purchase(db_session, branch, payload)


async def test_submit_purchase_non_numeric_external_ref_raises(db_session, branch: Branch):
    db_session.add(
        PurchaseSalesCache(
            branch_id=branch.id,
            spplr_tin="A987654321Z",
            spplr_invc_no="42",
            tot_amt=Decimal("116.00"),
            raw_data={"spplrTin": "A987654321Z", "spplrInvcNo": 42, "itemList": []},
        )
    )
    await db_session.flush()
    payload = ClientPurchaseIn(
        branch_code="VSCU-PURCH",
        external_ref="SAP-NOT-NUMERIC",
        spplr_tin="A987654321Z",
        spplr_invc_no="42",
        pchs_ty_cd="N",
    )
    with pytest.raises(InvalidExternalRef):
        await submit_purchase(db_session, branch, payload)


async def test_submit_purchase_enqueues_outbox_with_vscu_marker(db_session, branch: Branch):
    db_session.add(
        PurchaseSalesCache(
            branch_id=branch.id,
            spplr_tin="A987654321Z",
            spplr_invc_no="42",
            tot_amt=Decimal("116.00"),
            raw_data={
                "spplrTin": "A987654321Z",
                "spplrBhfId": "00",
                "spplrInvcNo": 42,
                "taxblAmtA": "0.00",
                "taxblAmtB": "100.00",
                "taxblAmtC": "0.00",
                "taxblAmtD": "0.00",
                "taxRtA": "0.00",
                "taxRtB": "16.00",
                "taxRtC": "0.00",
                "taxRtD": "0.00",
                "taxAmtA": "0.00",
                "taxAmtB": "16.00",
                "taxAmtC": "0.00",
                "taxAmtD": "0.00",
                "totTaxblAmt": "100.00",
                "totTaxAmt": "16.00",
                "totAmt": "116.00",
                "totItemCnt": 1,
                "itemList": [],
            },
        )
    )
    await db_session.flush()
    payload = ClientPurchaseIn(
        branch_code="VSCU-PURCH",
        external_ref="1001",
        spplr_tin="A987654321Z",
        spplr_invc_no="42",
        pchs_ty_cd="N",
    )
    purchase = await submit_purchase(db_session, branch, payload)

    assert purchase.status == "PENDING"
    outbox_row = await db_session.get(Outbox, purchase.outbox_id)
    assert outbox_row.endpoint == VSCU_SAVE_PURCHASE_ENDPOINT
    assert outbox_row.payload["invcNo"] == 1001
    assert outbox_row.payload["taxblAmtE"] == "0.00"

    cache = await db_session.scalar(
        select(PurchaseSalesCache).where(
            PurchaseSalesCache.branch_id == branch.id, PurchaseSalesCache.spplr_invc_no == "42"
        )
    )
    assert cache.confirmed is True
