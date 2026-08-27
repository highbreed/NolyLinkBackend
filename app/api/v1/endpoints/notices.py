"""GET /v1/notices, served from the local cache synced via /selectNoticeList (spec §18).

Not in spec §9's file tree (only items/branches/sales/purchases/stock/imports/codes/device
are listed there), but §18's endpoint table explicitly requires it.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.services import cmc_key_manager, notice_service

router = APIRouter()


class NoticeOut(BaseModel):
    branch_code: str
    notice_no: str
    title: str
    cont: str
    dtl_url: str | None
    regr_nm: str | None
    reg_dt: str | None


@router.get("", response_model=list[NoticeOut])
async def list_notices(
    branch_code: str,
    db: AsyncSession = Depends(get_db),
) -> list[NoticeOut]:
    try:
        branch = await cmc_key_manager.get_branch_credentials(db, branch_code)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    rows = await notice_service.get_notices(db, branch.id)
    return [
        NoticeOut(
            branch_code=branch.branch_code,
            notice_no=row.notice_no,
            title=row.title,
            cont=row.cont,
            dtl_url=row.dtl_url,
            regr_nm=row.regr_nm,
            reg_dt=row.reg_dt,
        )
        for row in rows
    ]
