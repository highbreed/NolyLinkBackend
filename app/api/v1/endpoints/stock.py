"""GET/POST /v1/stock/movements, POST /v1/stock/master (spec §18) — outbox pattern (spec
§14) for both POSTs: the business record (or, for stock master, just the outbox row — spec
gives no fields suggesting a richer local record is needed for a single itemCd/rsdQty pair)
is written in the same transaction as the outbox row, returning 202 Accepted immediately.
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Client, StockIOSubmission, StockMoveCache
from app.db.session import get_db
from app.schemas.client.stock_in import ClientStockIOIn, ClientStockMasterIn
from app.services import stock_service, vscu_stock_service
from app.services.client_auth import get_authorized_branch
from app.services.mapping.generic_mapper import CodeLookupError
from app.services.rate_limit import enforce_rate_limit

router = APIRouter()


class StockMoveOut(BaseModel):
    branch_code: str
    raw_data: dict[str, Any]
    synced_at: datetime


@router.get("/movements", response_model=list[StockMoveOut])
async def list_stock_movements(
    branch_code: str,
    client: Client = Depends(enforce_rate_limit),
    db: AsyncSession = Depends(get_db),
) -> list[StockMoveOut]:
    branch = await get_authorized_branch(db, client, branch_code)

    rows: list[StockMoveCache] = await stock_service.get_stock_moves(db, branch.id)
    return [
        StockMoveOut(branch_code=branch.branch_code, raw_data=row.raw_data, synced_at=row.synced_at)
        for row in rows
    ]


class StockIOOut(BaseModel):
    branch_code: str
    external_ref: str
    status: str


@router.post("/movements", response_model=StockIOOut, status_code=202)
async def submit_stock_movement(
    payload: ClientStockIOIn,
    client: Client = Depends(enforce_rate_limit),
    db: AsyncSession = Depends(get_db),
) -> StockIOOut:
    branch = await get_authorized_branch(db, client, payload.branch_code)

    try:
        submission: StockIOSubmission = (
            await vscu_stock_service.submit_stock_io(db, branch, payload)
            if branch.device_type == "VSCU"
            else await stock_service.submit_stock_io(db, branch, payload)
        )
    except CodeLookupError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except vscu_stock_service.InvalidExternalRef as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await db.commit()
    return StockIOOut(
        branch_code=branch.branch_code,
        external_ref=submission.external_ref,
        status=submission.status,
    )


@router.post("/master", status_code=202)
async def submit_stock_master(
    payload: ClientStockMasterIn,
    client: Client = Depends(enforce_rate_limit),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    branch = await get_authorized_branch(db, client, payload.branch_code)

    try:
        if branch.device_type == "VSCU":
            await vscu_stock_service.submit_stock_master(db, branch, payload)
        else:
            await stock_service.submit_stock_master(db, branch, payload)
    except CodeLookupError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await db.commit()
    return {"status": "queued"}
