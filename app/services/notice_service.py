"""Notice sync (spec §6.2, §11) — read-only, no Send side."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Branch, NoticeCache
from app.schemas.kra.codes import NoticeSearchReq
from app.services import sync_framework
from app.services.kra_adapter import KraAdapter

RESOURCE = "notices"


async def refresh_notices(db: AsyncSession, kra: KraAdapter, branch: Branch) -> None:
    last_req_dt = await sync_framework.get_last_req_dt(db, branch.tin, branch.bhf_id, RESOURCE)
    req = NoticeSearchReq(
        tin=branch.tin, bhfId=branch.bhf_id, cmcKey=branch.cmc_key, lastReqDt=last_req_dt
    )
    res = await kra.get_notice_list(req, branch_id=branch.id)

    if res.data is not None:
        for notice in res.data.noticeList:
            stmt = pg_insert(NoticeCache).values(
                branch_id=branch.id,
                notice_no=notice.noticeNo,
                title=notice.title,
                cont=notice.cont,
                dtl_url=notice.dtlUrl,
                regr_nm=notice.regrNm,
                reg_dt=notice.regDt,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[NoticeCache.branch_id, NoticeCache.notice_no],
                set_={
                    "title": stmt.excluded.title,
                    "cont": stmt.excluded.cont,
                    "dtl_url": stmt.excluded.dtl_url,
                    "regr_nm": stmt.excluded.regr_nm,
                    "reg_dt": stmt.excluded.reg_dt,
                    "synced_at": func.now(),
                },
            )
            await db.execute(stmt)

    await sync_framework.advance_last_req_dt(db, branch.tin, branch.bhf_id, RESOURCE, res.resultDt)


async def get_notices(db: AsyncSession, branch_id: uuid.UUID) -> list[NoticeCache]:
    result = await db.execute(
        select(NoticeCache)
        .where(NoticeCache.branch_id == branch_id)
        .order_by(NoticeCache.reg_dt.desc())
    )
    return list(result.scalars().all())
