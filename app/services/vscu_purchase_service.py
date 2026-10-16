"""VSCU purchase sync + confirmation. Mirrors app/services/purchase_service.py's shape (same
PurchaseSalesCache/Purchase tables, same sync_framework resource key "purchase_sales", same
outbox-dispatched confirmation pattern) but targets VSCU's wire DTOs
(app/integrations/vscu/schemas/purchase.py) via VSCUClient instead of KraAdapter.

Reuses PurchaseSalesCache/Purchase as-is (branch-scoped, protocol-agnostic in shape — a
branch is always exclusively OSCU or VSCU, so a cache row's raw_data is always consistent
with whichever protocol synced it). Confirmation is dispatched through the outbox
(app/workers/outbox_dispatcher.py), same as OSCU, since — like OSCU — a purchase confirmation
doesn't need an immediate signed response the way a sale does.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Branch, Purchase, PurchaseSalesCache
from app.integrations.vscu.client import VSCUClient
from app.integrations.vscu.schemas.purchase import TrnsPurchaseSalesReq
from app.schemas.client.purchase_in import ClientPurchaseIn
from app.services import outbox, sync_framework

RESOURCE = "purchase_sales"
VSCU_SAVE_PURCHASE_ENDPOINT = "/trnsPurchase/savePurchases"

DEFAULT_RCPT_TY_CD = "P"
DEFAULT_PCHS_STTS_CD = "02"
DEFAULT_REG_TY_CD = "M"

# See app/integrations/vscu/schemas/purchase.py's module docstring: the cached
# TrnsPurchaseSales entry never carries these tin/bhfId/invcNo-style identity or bucket-E
# fields — we own them, filled in fresh at confirmation time.
_FIELDS_WE_OWN = {
    "tin",
    "bhfId",
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
    "taxblAmtE",
    "taxRtE",
    "taxAmtE",
}


class PendingPurchaseNotFound(ValueError):
    pass


class InvalidExternalRef(ValueError):
    """VSCU's invcNo is a strict int (Long) on the wire — confirmed via decompiled
    TrnsPurchaseSaveReq — unlike OSCU's, which is a plain string. Same convention as OSCU
    otherwise (external_ref doubles as the KRA invcNo), but here that requires external_ref
    to already be numeric; raised at submission time rather than letting a non-numeric ref
    fail deep inside the outbox dispatcher's schema validation later.
    """


async def sync_purchase_sales(
    db: AsyncSession, client: VSCUClient, branch: Branch, correlation_id: uuid.UUID | None = None
) -> None:
    last_req_dt = await sync_framework.get_last_req_dt(db, branch.tin, branch.bhf_id, RESOURCE)
    req = TrnsPurchaseSalesReq(tin=branch.tin, bhfId=branch.bhf_id, lastReqDt=last_req_dt)
    res = await client.select_trns_purchase_sales(branch.id, req, correlation_id=correlation_id)

    if res.data is not None:
        for entry in res.data.saleList:
            stmt = pg_insert(PurchaseSalesCache).values(
                branch_id=branch.id,
                spplr_tin=entry.spplrTin,
                spplr_invc_no=str(entry.spplrInvcNo),
                tot_amt=entry.totAmt or Decimal("0.00"),
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

    if res.resultDt:
        await sync_framework.advance_last_req_dt(
            db, branch.tin, branch.bhf_id, RESOURCE, res.resultDt
        )


async def get_pending_purchases(db: AsyncSession, branch_id: uuid.UUID) -> list[PurchaseSalesCache]:
    result = await db.execute(
        select(PurchaseSalesCache)
        .where(PurchaseSalesCache.branch_id == branch_id, ~PurchaseSalesCache.confirmed)
        .order_by(PurchaseSalesCache.synced_at.desc())
    )
    return list(result.scalars().all())


async def submit_purchase(db: AsyncSession, branch: Branch, payload: ClientPurchaseIn) -> Purchase:
    """Enqueues via the outbox with a VSCU-flavored endpoint marker
    (VSCU_SAVE_PURCHASE_ENDPOINT) so outbox_dispatcher.py knows to dispatch this row through
    VSCUClient with VSCU's own TrnsPurchaseSaveReq schema, not KraAdapter/OSCU's.
    """
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
            f"spplr_invc_no={payload.spplr_invc_no!r} — sync via "
            f"POST /v1/vscu/{branch.id}/purchases/sync first"
        )

    if not payload.external_ref.isdigit():
        raise InvalidExternalRef(
            f"external_ref={payload.external_ref!r} must be numeric for a VSCU purchase "
            "(VSCU's invcNo is a strict integer, unlike OSCU's) — use a purely numeric "
            "external_ref for VSCU branches."
        )

    now = datetime.now(UTC)
    vscu_payload: dict[str, Any] = {
        k: v for k, v in cached.raw_data.items() if k not in _FIELDS_WE_OWN
    }
    vscu_payload.update(
        tin=branch.tin,
        bhfId=branch.bhf_id,
        invcNo=int(payload.external_ref),
        orgInvcNo=0,
        regTyCd=DEFAULT_REG_TY_CD,
        pchsTyCd=payload.pchs_ty_cd,
        rcptTyCd=DEFAULT_RCPT_TY_CD,
        pchsSttsCd=DEFAULT_PCHS_STTS_CD,
        pchsDt=now.strftime("%Y%m%d"),
        cfmDt=now.strftime("%Y%m%d%H%M%S"),
        taxblAmtE="0.00",
        taxRtE="0.00",
        taxAmtE="0.00",
        regrId="gateway",
        regrNm="VSCU Gateway",
        modrId="gateway",
        modrNm="VSCU Gateway",
    )
    for item in vscu_payload.get("itemList", []):
        item.setdefault("itemExprDt", None)

    purchase = Purchase(branch_id=branch.id, external_ref=payload.external_ref, status="PENDING")
    db.add(purchase)
    await db.flush()

    outbox_row = await outbox.enqueue(db, VSCU_SAVE_PURCHASE_ENDPOINT, branch.id, vscu_payload)
    purchase.outbox_id = outbox_row.id
    cached.confirmed = True

    return purchase
