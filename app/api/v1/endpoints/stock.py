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

from app.db.models import StockIOSubmission, StockMoveCache
from app.db.session import get_db
from app.schemas.client.stock_in import ClientStockIOIn, ClientStockMasterIn
from app.services import cmc_key_manager, stock_service
from app.services.mapping.generic_mapper import CodeLookupError

router = APIRouter()


class StockMoveOut(BaseModel):
    branch_code: str
    raw_data: dict[str, Any]
    synced_at: datetime


@router.get("/movements", response_model=list[StockMoveOut])
async def list_stock_movements(
    branch_code: str,
    db: AsyncSession = Depends(get_db),
) -> list[StockMoveOut]:
    try:
        branch = await cmc_key_manager.get_branch_credentials(db, branch_code)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

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
    db: AsyncSession = Depends(get_db),
) -> StockIOOut:
    try:
        branch = await cmc_key_manager.get_branch_credentials(db, payload.branch_code)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        submission: StockIOSubmission = await stock_service.submit_stock_io(db, branch, payload)
    except CodeLookupError as exc:
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
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    try:
        branch = await cmc_key_manager.get_branch_credentials(db, payload.branch_code)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        await stock_service.submit_stock_master(db, branch, payload)
    except CodeLookupError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await db.commit()
    return {"status": "queued"}
