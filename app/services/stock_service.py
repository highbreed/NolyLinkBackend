"""Stock move sync (spec §6.8, §11) and stock IO/master submission via the outbox (spec §14)."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Branch, Item, Outbox, StockIOSubmission, StockMoveCache
from app.schemas.client.stock_in import ClientStockIOIn, ClientStockMasterIn
from app.schemas.kra.stock import StockMoveReq
from app.services import outbox, sync_framework
from app.services.kra_adapter import KraAdapter
from app.services.mapping.generic_mapper import TAX_TY_CD_RATE, CodeLookupError

RESOURCE = "stock_moves"
STOCK_IO_ENDPOINT = "/insertStockIO"
STOCK_MASTER_ENDPOINT = "/saveStockMaster"

# spec §7's Registration Type (cdCls 31): A=Automatic, M=Manual — fully given, so this is
# spec-derived, same reasoning as purchase_service's DEFAULT_REG_TY_CD.
DEFAULT_REG_TY_CD = "M"


async def refresh_stock_moves(db: AsyncSession, kra: KraAdapter, branch: Branch) -> None:
    last_req_dt = await sync_framework.get_last_req_dt(db, branch.tin, branch.bhf_id, RESOURCE)
    req = StockMoveReq(
        tin=branch.tin, bhfId=branch.bhf_id, cmcKey=branch.cmc_key, lastReqDt=last_req_dt
    )
    res = await kra.get_stock_move_list(req, branch_id=branch.id)

    if res.data is not None:
        for entry in res.data.stockMoveList:
            db.add(StockMoveCache(branch_id=branch.id, raw_data=entry.model_dump(mode="json")))

    await sync_framework.advance_last_req_dt(db, branch.tin, branch.bhf_id, RESOURCE, res.resultDt)


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

    kra_payload: dict[str, object] = {
        "tin": branch.tin,
        "bhfId": branch.bhf_id,
        "cmcKey": branch.cmc_key,
        "sarNo": payload.external_ref,
        "orgSarNo": "0",
        "regTyCd": DEFAULT_REG_TY_CD,
        "sarTyCd": payload.sar_ty_cd,
        "ocrnDt": now.strftime("%Y%m%d"),
        "totItemCnt": len(item_lines),
        "totTaxblAmt": str(tot_taxbl_amt),
        "totTaxAmt": str(tot_tax_amt),
        "totAmt": str(tot_taxbl_amt + tot_tax_amt),
        "regrId": "gateway",
        "regrNm": "OSCU Gateway",
        "modrId": "gateway",
        "modrNm": "OSCU Gateway",
        "itemList": item_lines,
    }

    submission = StockIOSubmission(
        branch_id=branch.id, external_ref=payload.external_ref, status="PENDING"
    )
    db.add(submission)
    await db.flush()

    outbox_row = await outbox.enqueue(db, STOCK_IO_ENDPOINT, branch.id, kra_payload)
    submission.outbox_id = outbox_row.id

    return submission


async def submit_stock_master(
    db: AsyncSession, branch: Branch, payload: ClientStockMasterIn
) -> Outbox:
    item = await db.scalar(select(Item).where(Item.branch_id == branch.id, Item.sku == payload.sku))
    if item is None:
        raise CodeLookupError(f"No registered item for sku={payload.sku!r} — POST /v1/items first")

    kra_payload: dict[str, object] = {
        "tin": branch.tin,
        "bhfId": branch.bhf_id,
        "cmcKey": branch.cmc_key,
        "itemCd": item.item_cd,
        "rsdQty": str(payload.remaining_quantity),
    }
    return await outbox.enqueue(db, STOCK_MASTER_ENDPOINT, branch.id, kra_payload)
