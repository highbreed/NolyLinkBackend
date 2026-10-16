"""Tests for app/services/vscu_stock_service.py: sync into the shared StockMoveCache table,
and outbox-enqueued stock IO/master submission with VSCU's own field shapes (int sarNo, and
itemCd included on each line — confirmed required by decompiled StockIoItemSaveReq, unlike
the OSCU stock_service.submit_stock_io code path which omits it).
"""

from decimal import Decimal

import pytest
import respx
from httpx import Response
from sqlalchemy import select

from app.core.config import Settings
from app.db.models import Branch, Item, Outbox, StockMoveCache, VscuDeviceState
from app.integrations.vscu.client import VSCUClient
from app.schemas.client.stock_in import ClientStockIOIn, ClientStockIOLine, ClientStockMasterIn
from app.services.mapping.generic_mapper import CodeLookupError
from app.services.vscu_stock_service import (
    VSCU_STOCK_IO_ENDPOINT,
    VSCU_STOCK_MASTER_ENDPOINT,
    InvalidExternalRef,
    submit_stock_io,
    submit_stock_master,
    sync_stock_moves,
)

VSCU_URL = "http://vscu-branch.test:8088"


@pytest.fixture
def vscu_client(db_session) -> VSCUClient:
    return VSCUClient(db=db_session, settings=Settings(vscu_enabled=True))


@pytest.fixture
async def branch(db_session) -> Branch:
    b = Branch(
        branch_code="VSCU-STOCK",
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
    await db_session.flush()
    return b


async def test_sync_stock_moves_writes_shared_cache(
    db_session, branch: Branch, vscu_client: VSCUClient
):
    with respx.mock:
        respx.post(f"{VSCU_URL}/stock/selectStockItems").mock(
            return_value=Response(
                200,
                json={
                    "resultCd": "000",
                    "resultMsg": "ok",
                    "resultDt": "20260901120000",
                    "data": {"stockList": [{"sarNo": 1, "totItemCnt": 0, "itemList": []}]},
                },
            )
        )
        await sync_stock_moves(db_session, vscu_client, branch)

    rows = (
        (
            await db_session.execute(
                select(StockMoveCache).where(StockMoveCache.branch_id == branch.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].raw_data["sarNo"] == 1


async def test_submit_stock_io_non_numeric_external_ref_raises(db_session, branch: Branch):
    payload = ClientStockIOIn(
        branch_code="VSCU-STOCK",
        external_ref="SAP-NOT-NUMERIC",
        sar_ty_cd="01",
        lines=[ClientStockIOLine(sku="BREAD", quantity=Decimal("2"), unit_price=Decimal("100.00"))],
    )
    with pytest.raises(InvalidExternalRef):
        await submit_stock_io(db_session, branch, payload)


async def test_submit_stock_io_unregistered_sku_raises_code_lookup_error(
    db_session, branch: Branch
):
    payload = ClientStockIOIn(
        branch_code="VSCU-STOCK",
        external_ref="1001",
        sar_ty_cd="01",
        lines=[ClientStockIOLine(sku="UNKNOWN", quantity=Decimal("1"), unit_price=Decimal("1.00"))],
    )
    with pytest.raises(CodeLookupError):
        await submit_stock_io(db_session, branch, payload)


async def test_submit_stock_io_enqueues_outbox_with_item_cd(db_session, branch: Branch):
    payload = ClientStockIOIn(
        branch_code="VSCU-STOCK",
        external_ref="1001",
        sar_ty_cd="01",
        lines=[ClientStockIOLine(sku="BREAD", quantity=Decimal("2"), unit_price=Decimal("100.00"))],
    )
    submission = await submit_stock_io(db_session, branch, payload)

    assert submission.status == "PENDING"
    outbox_row = await db_session.get(Outbox, submission.outbox_id)
    assert outbox_row.endpoint == VSCU_STOCK_IO_ENDPOINT
    assert outbox_row.payload["sarNo"] == 1001
    assert outbox_row.payload["itemList"][0]["itemCd"] == "KE2NTBA0000001"
    assert outbox_row.payload["totTaxAmt"] == "32.00"


async def test_submit_stock_master_enqueues_outbox(db_session, branch: Branch):
    payload = ClientStockMasterIn(
        branch_code="VSCU-STOCK", sku="BREAD", remaining_quantity=Decimal("50")
    )
    outbox_row = await submit_stock_master(db_session, branch, payload)

    assert outbox_row.endpoint == VSCU_STOCK_MASTER_ENDPOINT
    assert outbox_row.payload["itemCd"] == "KE2NTBA0000001"
    assert outbox_row.payload["rsdQty"] == "50"
