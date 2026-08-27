"""Purchase-sales sync (spec §6.7, §11) and purchase confirmation via the outbox (spec §14)."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Branch, Purchase, PurchaseSalesCache
from app.schemas.client.purchase_in import ClientPurchaseIn
from app.schemas.kra.purchase import TrnsPurchaseSalesReq
from app.services import outbox, sync_framework
from app.services.kra_adapter import KraAdapter

RESOURCE = "purchase_sales"
ENDPOINT = "/insertTrnsPurchase"

# spec §7's Purchase Receipt Type (cdCls 38) is fully given (P=Purchase, R=Credit Note after
# Purchase), so "P" here is spec-derived. Transaction Progress (cdCls 11) is only partially
# given ("01 Wait for Approval … 06 Transferred"), same gap as sales' salesSttsCd (Phase 7) —
# DEFAULT_PCHS_STTS_CD is a best-effort default pending sandbox confirmation. Registration
# Type (cdCls 31, A=Automatic/M=Manual) is fully given: "M" because this is a human/API-
# triggered confirmation, not something KRA auto-detected.
DEFAULT_RCPT_TY_CD = "P"
DEFAULT_PCHS_STTS_CD = "02"
DEFAULT_REG_TY_CD = "M"

# Fields the cached raw_data (a verbatim PurchaseSalesEntry) already carries and that we must
# NOT recompute — tax buckets, supplier identity, and the item list all come from what KRA
# itself declared, not from us. Only these get overwritten with values we own.
_FIELDS_WE_OWN = {
    "tin",
    "bhfId",
    "cmcKey",
    "invcNo",
    "orgInvcNo",
    "regTyCd",
    "pchsTyCd",
    "rcptTyCd",
    "pchsSttsCd",
    "pchsDt",
    "cfmDt",
    "regrId",
    "regrNm",
    "modrId",
    "modrNm",
}


class PendingPurchaseNotFound(ValueError):
    pass


async def refresh_purchase_sales(db: AsyncSession, kra: KraAdapter, branch: Branch) -> None:
    last_req_dt = await sync_framework.get_last_req_dt(db, branch.tin, branch.bhf_id, RESOURCE)
    req = TrnsPurchaseSalesReq(
        tin=branch.tin, bhfId=branch.bhf_id, cmcKey=branch.cmc_key, lastReqDt=last_req_dt
    )
    res = await kra.get_purchase_sales_list(req, branch_id=branch.id)

    if res.data is not None:
        for entry in res.data.saleList:
            stmt = pg_insert(PurchaseSalesCache).values(
                branch_id=branch.id,
                spplr_tin=entry.spplrTin,
                spplr_invc_no=entry.spplrInvcNo,
                tot_amt=entry.totAmt,
                raw_data=entry.model_dump(mode="json"),
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[
                    PurchaseSalesCache.branch_id,
                    PurchaseSalesCache.spplr_tin,
                    PurchaseSalesCache.spplr_invc_no,
                ],
                set_={
                    "tot_amt": stmt.excluded.tot_amt,
                    "raw_data": stmt.excluded.raw_data,
                    "synced_at": func.now(),
                },
            )
            await db.execute(stmt)

    await sync_framework.advance_last_req_dt(db, branch.tin, branch.bhf_id, RESOURCE, res.resultDt)


async def get_pending_purchases(db: AsyncSession, branch_id: uuid.UUID) -> list[PurchaseSalesCache]:
    result = await db.execute(
        select(PurchaseSalesCache)
        .where(PurchaseSalesCache.branch_id == branch_id, ~PurchaseSalesCache.confirmed)
        .order_by(PurchaseSalesCache.synced_at.desc())
    )
    return list(result.scalars().all())


async def submit_purchase(db: AsyncSession, branch: Branch, payload: ClientPurchaseIn) -> Purchase:
    cached = await db.scalar(
        select(PurchaseSalesCache).where(
            PurchaseSalesCache.branch_id == branch.id,
            PurchaseSalesCache.spplr_tin == payload.spplr_tin,
            PurchaseSalesCache.spplr_invc_no == payload.spplr_invc_no,
        )
    )
    if cached is None:
        raise PendingPurchaseNotFound(
            f"No pending purchase-sale cached for spplr_tin={payload.spplr_tin!r} "
            f"spplr_invc_no={payload.spplr_invc_no!r} — sync via Phase 5's poller first"
        )

    now = datetime.now(UTC)
    kra_payload: dict[str, Any] = {
        k: v for k, v in cached.raw_data.items() if k not in _FIELDS_WE_OWN
    }
    kra_payload.update(
        tin=branch.tin,
        bhfId=branch.bhf_id,
        cmcKey=branch.cmc_key,
        invcNo=payload.external_ref,
        orgInvcNo="0",
        regTyCd=DEFAULT_REG_TY_CD,
        pchsTyCd=payload.pchs_ty_cd,
        rcptTyCd=DEFAULT_RCPT_TY_CD,
        pchsSttsCd=DEFAULT_PCHS_STTS_CD,
        pchsDt=now.strftime("%Y%m%d"),
        cfmDt=now.strftime("%Y%m%d%H%M%S"),
        regrId="gateway",
        regrNm="OSCU Gateway",
        modrId="gateway",
        modrNm="OSCU Gateway",
    )
    for item in kra_payload.get("itemList", []):
        item.setdefault("itemExprDt", None)

    purchase = Purchase(branch_id=branch.id, external_ref=payload.external_ref, status="PENDING")
    db.add(purchase)
    await db.flush()

    outbox_row = await outbox.enqueue(db, ENDPOINT, branch.id, kra_payload)
    purchase.outbox_id = outbox_row.id
    cached.confirmed = True

    return purchase
