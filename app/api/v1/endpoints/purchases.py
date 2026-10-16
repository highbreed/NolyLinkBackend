"""GET /v1/purchases/pending, POST /v1/purchases (spec §18) — outbox pattern (spec §14):
POST writes the Purchase business record and its outbox row in the same transaction and
returns 202 Accepted immediately.

GET /pending needs no protocol dispatch: PurchaseSalesCache is the same table regardless of
device_type (app/services/vscu_purchase_service.py reuses it as-is), so purchase_service's
read here already works transparently for VSCU branches. POST dispatches by device_type,
same pattern as app/api/v1/endpoints/sales.py — the OSCU path below is unchanged.
"""

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Client, Purchase, PurchaseSalesCache
from app.db.session import get_db
from app.schemas.client.purchase_in import ClientPurchaseIn
from app.services import purchase_service, vscu_purchase_service
from app.services.client_auth import get_authorized_branch
from app.services.rate_limit import enforce_rate_limit

router = APIRouter()


class PendingPurchaseOut(BaseModel):
    branch_code: str
    spplr_tin: str
    spplr_invc_no: str
    tot_amt: Decimal
    item_list: list[dict[str, Any]]


def _pending_to_out(row: PurchaseSalesCache, branch_code: str) -> PendingPurchaseOut:
    return PendingPurchaseOut(
        branch_code=branch_code,
        spplr_tin=row.spplr_tin,
        spplr_invc_no=row.spplr_invc_no,
        tot_amt=row.tot_amt,
        item_list=row.raw_data.get("itemList", []),
    )


@router.get("/pending", response_model=list[PendingPurchaseOut])
async def list_pending_purchases(
    branch_code: str,
    client: Client = Depends(enforce_rate_limit),
    db: AsyncSession = Depends(get_db),
) -> list[PendingPurchaseOut]:
    branch = await get_authorized_branch(db, client, branch_code)

    rows = await purchase_service.get_pending_purchases(db, branch.id)
    return [_pending_to_out(row, branch.branch_code) for row in rows]


class PurchaseOut(BaseModel):
    branch_code: str
    external_ref: str
    status: str


@router.post("", response_model=PurchaseOut, status_code=202)
async def submit_purchase(
    payload: ClientPurchaseIn,
    client: Client = Depends(enforce_rate_limit),
    db: AsyncSession = Depends(get_db),
) -> PurchaseOut:
    branch = await get_authorized_branch(db, client, payload.branch_code)

    try:
        purchase: Purchase = (
            await vscu_purchase_service.submit_purchase(db, branch, payload)
            if branch.device_type == "VSCU"
            else await purchase_service.submit_purchase(db, branch, payload)
        )
    except (
        purchase_service.PendingPurchaseNotFound,
        vscu_purchase_service.PendingPurchaseNotFound,
    ) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except vscu_purchase_service.InvalidExternalRef as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await db.commit()
    return PurchaseOut(
        branch_code=branch.branch_code, external_ref=purchase.external_ref, status=purchase.status
    )
