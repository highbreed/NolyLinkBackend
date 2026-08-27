"""Tests for stock_service (spec §6.8, §11, §14)."""

import json
from decimal import Decimal
from pathlib import Path

import pytest
import respx
from httpx import Response

from app.core.config import Settings
from app.db.models import Branch, Item, Outbox, StockIOSubmission
from app.schemas.client.stock_in import ClientStockIOIn, ClientStockIOLine, ClientStockMasterIn
from app.services import stock_service
from app.services.kra_adapter import KraAdapter
from app.services.mapping.generic_mapper import CodeLookupError

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


async def test_refresh_stock_moves_stores_raw_data(db_session, kra, branch):
    res_body = load_fixture("stock_move_res")

    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/selectStockMoveList").mock(return_value=Response(200, json=res_body))
        await stock_service.refresh_stock_moves(db_session, kra, branch)

    rows = await stock_service.get_stock_moves(db_session, branch.id)
    assert len(rows) == 1
    assert rows[0].raw_data == res_body["data"]["stockMoveList"][0]


async def test_submit_stock_io_computes_tax_from_item_rate(db_session, branch):
    payload = ClientStockIOIn(
        branch_code="NAIROBI-HQ",
        external_ref="SIO-0001",
        sar_ty_cd="02",
        lines=[ClientStockIOLine(sku="BREAD", quantity=Decimal("2"), unit_price=Decimal("100.00"))],
    )
    submission = await stock_service.submit_stock_io(db_session, branch, payload)

    assert isinstance(submission, StockIOSubmission)
    assert submission.status == "PENDING"

    outbox_row = await db_session.get(Outbox, submission.outbox_id)
    assert outbox_row.endpoint == "/insertStockIO"
    body = outbox_row.payload
    assert body["totTaxblAmt"] == "200.00"
    assert body["totTaxAmt"] == "32.00"
    assert body["totAmt"] == "232.00"
    assert body["itemList"][0]["itemClsCd"] == "5020150101"
    assert body["itemList"][0]["pkgUnitCd"] == "NT"


async def test_submit_stock_io_unregistered_sku_raises(db_session, branch):
    payload = ClientStockIOIn(
        branch_code="NAIROBI-HQ",
        external_ref="SIO-0002",
        sar_ty_cd="02",
        lines=[ClientStockIOLine(sku="GHOST", quantity=Decimal("1"), unit_price=Decimal("1.00"))],
    )
    with pytest.raises(CodeLookupError):
        await stock_service.submit_stock_io(db_session, branch, payload)


async def test_submit_stock_master_uses_registered_item_cd(db_session, branch):
    payload = ClientStockMasterIn(
        branch_code="NAIROBI-HQ", sku="BREAD", remaining_quantity=Decimal("48")
    )
    outbox_row = await stock_service.submit_stock_master(db_session, branch, payload)

    assert outbox_row.endpoint == "/saveStockMaster"
    assert outbox_row.payload["itemCd"] == "KE2NTBA0000001"
    assert outbox_row.payload["rsdQty"] == "48"
