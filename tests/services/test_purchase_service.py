"""Tests for purchase_service (spec §6.7, §11, §14) — especially that tax buckets sent back
to KRA on confirmation come verbatim from what KRA itself declared, never recomputed.
"""

import json
from pathlib import Path

import pytest
import respx
from httpx import Response

from app.core.config import Settings
from app.db.models import Branch, Outbox, Purchase, PurchaseSalesCache
from app.schemas.client.purchase_in import ClientPurchaseIn
from app.services import purchase_service
from app.services.kra_adapter import KraAdapter

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "kra_samples"
BASE_URL = "https://kra-sandbox.test/etims-api"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text())


@pytest.fixture
def kra(db_session) -> KraAdapter:
    return KraAdapter(
        db=db_session, settings=Settings(kra_env="sandbox", kra_base_url_sbx=f"{BASE_URL}/")
    )


@pytest.fixture
async def branch(db_session) -> Branch:
    b = Branch(branch_code="NAIROBI-HQ", tin="A123456789Z", bhf_id="00", cmc_key="c" * 10)
    db_session.add(b)
    await db_session.flush()
    return b


async def test_refresh_stores_full_raw_entry(db_session, kra, branch):
    res_body = load_fixture("trns_purchase_sales_res")

    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/selectTrnsPurchaseSalesList").mock(return_value=Response(200, json=res_body))
        await purchase_service.refresh_purchase_sales(db_session, kra, branch)

    pending = await purchase_service.get_pending_purchases(db_session, branch.id)
    assert len(pending) == 1
    entry = pending[0]
    assert entry.spplr_tin == "P987654321B"
    # Every tax bucket KRA declared must round-trip into raw_data untouched.
    assert entry.raw_data["taxblAmtB"] == "200.00"
    assert entry.raw_data["taxAmtB"] == "32.00"
    assert entry.raw_data["taxRtB"] == "16.00"
    assert entry.confirmed is False


async def test_submit_purchase_reuses_declared_tax_buckets_verbatim(db_session, branch):
    cached = PurchaseSalesCache(
        branch_id=branch.id,
        spplr_tin="P987654321B",
        spplr_invc_no="SUP-INV-0001",
        tot_amt="232.00",
        raw_data={
            "spplrTin": "P987654321B",
            "spplrBhfId": "00",
            "spplrInvcNo": "SUP-INV-0001",
            "rcptTyCd": "S",
            "pmtTyCd": "01",
            "cfmDt": "20200226143124",
            "salesDt": "20200226",
            "stockRlsDt": None,
            "totItemCnt": 1,
            "taxblAmtA": "0.00",
            "taxblAmtB": "200.00",
            "taxblAmtC": "0.00",
            "taxblAmtD": "0.00",
            "taxblAmtE": "0.00",
            "taxRtA": "0.00",
            "taxRtB": "16.00",
            "taxRtC": "0.00",
            "taxRtD": "0.00",
            "taxRtE": "0.00",
            "taxAmtA": "0.00",
            "taxAmtB": "32.00",
            "taxAmtC": "0.00",
            "taxAmtD": "0.00",
            "taxAmtE": "0.00",
            "totTaxblAmt": "200.00",
            "totTaxAmt": "32.00",
            "totAmt": "232.00",
            "itemList": [
                {
                    "itemSeq": 1,
                    "itemCd": "KE2NTBA0000001",
                    "itemNm": "Bread",
                    "pkgUnitCd": "NT",
                    "pkg": "1.00",
                    "qtyUnitCd": "BA",
                    "qty": "2.00",
                    "prc": "100.00",
                    "splyAmt": "200.00",
                    "dcRt": "0.00",
                    "dcAmt": "0.00",
                    "taxTyCd": "B",
                    "taxblAmt": "200.00",
                    "totTaxAmt": "32.00",
                    "totAmt": "232.00",
                }
            ],
        },
    )
    db_session.add(cached)
    await db_session.flush()

    payload = ClientPurchaseIn(
        branch_code="NAIROBI-HQ",
        external_ref="PO-0001",
        spplr_tin="P987654321B",
        spplr_invc_no="SUP-INV-0001",
        pchs_ty_cd="N",
    )
    purchase = await purchase_service.submit_purchase(db_session, branch, payload)

    assert isinstance(purchase, Purchase)
    assert purchase.status == "PENDING"
    assert purchase.outbox_id is not None

    outbox_row = await db_session.get(Outbox, purchase.outbox_id)
    assert outbox_row.endpoint == "/insertTrnsPurchase"
    body = outbox_row.payload
    # The buckets KRA declared must appear verbatim, not recomputed.
    assert body["taxblAmtB"] == "200.00"
    assert body["taxAmtB"] == "32.00"
    assert body["totAmt"] == "232.00"
    assert body["itemList"][0]["itemCd"] == "KE2NTBA0000001"
    assert body["itemList"][0]["itemExprDt"] is None
    # Fields the gateway owns are overwritten, not taken from raw_data.
    assert body["invcNo"] == "PO-0001"
    assert body["pchsTyCd"] == "N"
    assert body["regTyCd"] == "M"

    cached_after = await db_session.get(PurchaseSalesCache, cached.id)
    assert cached_after.confirmed is True


async def test_submit_purchase_unknown_supplier_invoice_raises(db_session, branch):
    payload = ClientPurchaseIn(
        branch_code="NAIROBI-HQ",
        external_ref="PO-0002",
        spplr_tin="P000000000X",
        spplr_invc_no="DOES-NOT-EXIST",
        pchs_ty_cd="N",
    )
    with pytest.raises(purchase_service.PendingPurchaseNotFound):
        await purchase_service.submit_purchase(db_session, branch, payload)
