"""GET /v1/imports (cached), POST /v1/imports/{taskCd}/decision (spec §18) — outbox pattern
(spec §14): the decision is written to outbox in the same transaction as the cache-row
update, and 202 Accepted is returned immediately.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ImportItemCache
from app.db.session import get_db
from app.services import cmc_key_manager, import_item_service

router = APIRouter()


class ImportItemOut(BaseModel):
    branch_code: str
    task_cd: str
    dcl_de: str
    item_seq: int
    dcl_no: str
    hs_cd: str
    item_nm: str
    orgn_nat_cd: str
    impt_item_stts_cd: str
    synced_at: datetime


def _to_out(row: ImportItemCache, branch_code: str) -> ImportItemOut:
    return ImportItemOut(
        branch_code=branch_code,
        task_cd=row.task_cd,
        dcl_de=row.dcl_de,
        item_seq=row.item_seq,
        dcl_no=row.dcl_no,
        hs_cd=row.hs_cd,
        item_nm=row.item_nm,
        orgn_nat_cd=row.orgn_nat_cd,
        impt_item_stts_cd=row.impt_item_stts_cd,
        synced_at=row.synced_at,
    )


@router.get("", response_model=list[ImportItemOut])
async def list_imports(
    branch_code: str,
    db: AsyncSession = Depends(get_db),
) -> list[ImportItemOut]:
    try:
        branch = await cmc_key_manager.get_branch_credentials(db, branch_code)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    rows = await import_item_service.get_pending_imports(db, branch.id)
    return [_to_out(row, branch.branch_code) for row in rows]


class ImportDecisionIn(BaseModel):
    branch_code: str
    item_cls_cd: str
    item_cd: str
    decision_cd: str  # spec §12.16: 1 Unsent / 2 Waiting / 3 Approved / 4 Cancelled


@router.post("/{task_cd}/decision", status_code=202)
async def decide_import_item(
    task_cd: str,
    payload: ImportDecisionIn,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    try:
        branch = await cmc_key_manager.get_branch_credentials(db, payload.branch_code)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        await import_item_service.submit_import_decision(
            db, branch, task_cd, payload.item_cls_cd, payload.item_cd, payload.decision_cd
        )
    except import_item_service.ImportItemNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    await db.commit()
    return {"status": "queued"}
