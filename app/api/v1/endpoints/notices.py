"""GET /v1/notices, served from the local cache synced via /selectNoticeList (spec §18).

Not in spec §9's file tree (only items/branches/sales/purchases/stock/imports/codes/device
are listed there), but §18's endpoint table explicitly requires it.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Client
from app.db.session import get_db
from app.services import notice_service
from app.services.client_auth import get_authorized_branch
from app.services.rate_limit import enforce_rate_limit

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
    client: Client = Depends(enforce_rate_limit),
    db: AsyncSession = Depends(get_db),
) -> list[NoticeOut]:
    branch = await get_authorized_branch(db, client, branch_code)

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
