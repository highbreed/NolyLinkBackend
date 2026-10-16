"""VSCU stock move sync + stock IO/master submission. Mirrors app/services/stock_service.py's
shape (same StockMoveCache/StockIOSubmission tables, same "stock_moves" sync_framework
resource key, same outbox-dispatched submission pattern) but targets VSCU's wire DTOs
(app/integrations/vscu/schemas/stock.py) via VSCUClient instead of KraAdapter.
"""

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Branch, Item, Outbox, StockIOSubmission, StockMoveCache
from app.integrations.vscu.client import VSCUClient
from app.integrations.vscu.exceptions import VSCUError
from app.integrations.vscu.schemas.stock import StockMove, StockMoveReq
from app.schemas.client.stock_in import ClientStockIOIn, ClientStockMasterIn
from app.services import outbox, sync_framework
from app.services.mapping.generic_mapper import TAX_TY_CD_RATE, CodeLookupError

logger = logging.getLogger(__name__)

RESOURCE = "stock_moves"
VSCU_STOCK_IO_ENDPOINT = "/stock/saveStockItems"
VSCU_STOCK_MASTER_ENDPOINT = "/stockMaster/saveStockMaster"

DEFAULT_REG_TY_CD = "M"


class InvalidExternalRef(ValueError):
    """VSCU's sarNo is a strict int on the wire (confirmed via decompiled StockIoSaveReq),
    unlike OSCU's, which is a plain string — same reasoning as
    app/services/vscu_purchase_service.py's InvalidExternalRef.
    """


async def sync_stock_moves(
    db: AsyncSession, client: VSCUClient, branch: Branch, correlation_id: uuid.UUID | None = None
) -> None:
    last_req_dt = await sync_framework.get_last_req_dt(db, branch.tin, branch.bhf_id, RESOURCE)
    req = StockMoveReq(tin=branch.tin, bhfId=branch.bhf_id, lastReqDt=last_req_dt)
    res = await client.select_stock_items(branch.id, req, correlation_id=correlation_id)

    if res.data is not None:
        for entry in res.data.stockList:
            db.add(StockMoveCache(branch_id=branch.id, raw_data=entry.model_dump(mode="json")))

    if res.resultDt:
        await sync_framework.advance_last_req_dt(
            db, branch.tin, branch.bhf_id, RESOURCE, res.resultDt
        )


async def reconcile_by_sar_no(
    client: VSCUClient,
    branch: Branch,
    sar_no: int,
    correlation_id: uuid.UUID | None = None,
) -> StockMove | None:
    """General-purpose reconciliation for a specific stock movement: look up sar_no via
    /stock/selectStockItems — a plain proxy, real KRA's resultCd is trusted here (unlike
    /stock/saveStockItems's confirmed 894/896 local-resend quirk, see
    app/integrations/vscu/schemas/stock.py's module docstring) — to confirm whether VSCU
    actually holds a stock movement under that sarNo, rather than trusting
    StockIOSubmission.status="SENT" (set purely from saveStockItems's own unconditionally-
    rewritten "000"). Mirrors VSCUSalesService.reconcile_by_invoice_number's shape: a
    side-channel diagnostic lookup, not tied to the regular "stock_moves" sync_framework
    watermark — it always searches from kra_sync_initial_last_req_dt so sar_no is found
    regardless of where that separate incremental poller's watermark currently sits, and
    never advances it (this is a point lookup for ops, not a sync run).
    """
    req = StockMoveReq(
        tin=branch.tin,
        bhfId=branch.bhf_id,
        lastReqDt=get_settings().kra_sync_initial_last_req_dt,
    )
    try:
        res = await client.select_stock_items(branch.id, req, correlation_id=correlation_id)
    except VSCUError:
        logger.exception(
            "Stock reconciliation lookup failed for branch=%s sarNo=%s", branch.id, sar_no
        )
        return None

    if res.data is None:
        return None
    return next((s for s in res.data.stockList if s.sarNo == sar_no), None)


async def get_stock_moves(db: AsyncSession, branch_id: uuid.UUID) -> list[StockMoveCache]:
    result = await db.execute(
        select(StockMoveCache)
        .where(StockMoveCache.branch_id == branch_id)
        .order_by(StockMoveCache.synced_at.desc())
    )
    return list(result.scalars().all())


async def submit_stock_io(
    db: AsyncSession, branch: Branch, payload: ClientStockIOIn
) -> StockIOSubmission:
    if not payload.external_ref.isdigit():
        raise InvalidExternalRef(
            f"external_ref={payload.external_ref!r} must be numeric for a VSCU stock "
            "movement (VSCU's sarNo is a strict integer, unlike OSCU's)."
        )

    now = datetime.now(UTC)
    item_lines = []
    tot_taxbl_amt = Decimal("0.00")
    tot_tax_amt = Decimal("0.00")
    for i, line in enumerate(payload.lines, start=1):
        item = await db.scalar(
            select(Item).where(Item.branch_id == branch.id, Item.sku == line.sku)
        )
        if item is None:
            raise CodeLookupError(f"No registered item for sku={line.sku!r} — POST /v1/items first")
        sply_amt = line.quantity * line.unit_price
        tax_amt = (sply_amt * TAX_TY_CD_RATE[item.tax_ty_cd] / 100).quantize(Decimal("0.01"))
        tot_taxbl_amt += sply_amt
        tot_tax_amt += tax_amt
        item_lines.append(
            {
                "itemSeq": i,
                "itemCd": item.item_cd,
                "itemClsCd": item.item_cls_cd,
                "itemNm": item.item_nm,
                "pkgUnitCd": item.pkg_unit_cd,
                "pkg": str(line.quantity),
                "qtyUnitCd": item.qty_unit_cd,
                "qty": str(line.quantity),
                "prc": str(line.unit_price),
                "splyAmt": str(sply_amt),
                "totDcAmt": "0.00",
                "taxblAmt": str(sply_amt),
                "taxTyCd": item.tax_ty_cd,
                "taxAmt": str(tax_amt),
                "totAmt": str(sply_amt + tax_amt),
            }
        )

    vscu_payload: dict[str, object] = {
        "tin": branch.tin,
        "bhfId": branch.bhf_id,
        "sarNo": int(payload.external_ref),
        "orgSarNo": 0,
        "regTyCd": DEFAULT_REG_TY_CD,
        "sarTyCd": payload.sar_ty_cd,
        "ocrnDt": now.strftime("%Y%m%d"),
        "totItemCnt": len(item_lines),
        "totTaxblAmt": str(tot_taxbl_amt),
        "totTaxAmt": str(tot_tax_amt),
        "totAmt": str(tot_taxbl_amt + tot_tax_amt),
        "regrId": "gateway",
        "regrNm": "VSCU Gateway",
        "modrId": "gateway",
        "modrNm": "VSCU Gateway",
        "itemList": item_lines,
    }

    submission = StockIOSubmission(
        branch_id=branch.id, external_ref=payload.external_ref, status="PENDING"
    )
    db.add(submission)
    await db.flush()

    outbox_row = await outbox.enqueue(db, VSCU_STOCK_IO_ENDPOINT, branch.id, vscu_payload)
    submission.outbox_id = outbox_row.id

    return submission


async def submit_stock_master(
    db: AsyncSession, branch: Branch, payload: ClientStockMasterIn
) -> Outbox:
    item = await db.scalar(select(Item).where(Item.branch_id == branch.id, Item.sku == payload.sku))
    if item is None:
        raise CodeLookupError(f"No registered item for sku={payload.sku!r} — POST /v1/items first")

    vscu_payload: dict[str, object] = {
        "tin": branch.tin,
        "bhfId": branch.bhf_id,
        "itemCd": item.item_cd,
        "rsdQty": str(payload.remaining_quantity),
        "regrId": "gateway",
        "regrNm": "VSCU Gateway",
        "modrId": "gateway",
        "modrNm": "VSCU Gateway",
    }
    return await outbox.enqueue(db, VSCU_STOCK_MASTER_ENDPOINT, branch.id, vscu_payload)
