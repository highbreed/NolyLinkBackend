"""Import item sync (spec §6.5, §11) and decision submission via the outbox (spec §14)."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Branch, ImportItemCache
from app.schemas.kra.import_item import ImportItemSearchReq
from app.services import outbox, sync_framework
from app.services.kra_adapter import KraAdapter

RESOURCE = "import_items"
ENDPOINT = "/updateImportItem"


class ImportItemNotFound(ValueError):
    pass


async def refresh_import_items(db: AsyncSession, kra: KraAdapter, branch: Branch) -> None:
    last_req_dt = await sync_framework.get_last_req_dt(db, branch.tin, branch.bhf_id, RESOURCE)
    req = ImportItemSearchReq(
        tin=branch.tin, bhfId=branch.bhf_id, cmcKey=branch.cmc_key, lastReqDt=last_req_dt
    )
    res = await kra.get_import_item_list(req, branch_id=branch.id)

    if res.data is not None:
        for entry in res.data.itemList:
            stmt = pg_insert(ImportItemCache).values(
                branch_id=branch.id,
                task_cd=entry.taskCd,
                dcl_de=entry.dclDe,
                item_seq=entry.itemSeq,
                dcl_no=entry.dclNo,
                hs_cd=entry.hsCd,
                item_nm=entry.itemNm,
                orgn_nat_cd=entry.orgnNatCd,
                raw_data=entry.model_dump(mode="json"),
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[
                    ImportItemCache.branch_id,
                    ImportItemCache.task_cd,
                    ImportItemCache.dcl_de,
                    ImportItemCache.item_seq,
                ],
                set_={
                    "dcl_no": stmt.excluded.dcl_no,
                    "hs_cd": stmt.excluded.hs_cd,
                    "item_nm": stmt.excluded.item_nm,
                    "orgn_nat_cd": stmt.excluded.orgn_nat_cd,
                    "raw_data": stmt.excluded.raw_data,
                    "synced_at": func.now(),
                },
            )
            await db.execute(stmt)

    await sync_framework.advance_last_req_dt(db, branch.tin, branch.bhf_id, RESOURCE, res.resultDt)


async def get_pending_imports(db: AsyncSession, branch_id: uuid.UUID) -> list[ImportItemCache]:
    result = await db.execute(
        select(ImportItemCache)
        .where(ImportItemCache.branch_id == branch_id)
        .order_by(ImportItemCache.synced_at.desc())
    )
    return list(result.scalars().all())


async def submit_import_decision(
    db: AsyncSession,
    branch: Branch,
    task_cd: str,
    item_cls_cd: str,
    item_cd: str,
    decision_cd: str,
) -> ImportItemCache:
    """decision_cd per spec §12.16: 1 Unsent / 2 Waiting / 3 Approved / 4 Cancelled.

    Applies to the first cached row for task_cd — the endpoint path (spec §18) only carries
    taskCd, not the full (dclDe, itemSeq) natural key, so this assumes taskCd is unique
    enough in practice; a genuinely multi-item declaration would need those disambiguated,
    which isn't possible from the path alone.
    """
    cached = await db.scalar(
        select(ImportItemCache).where(
            ImportItemCache.branch_id == branch.id, ImportItemCache.task_cd == task_cd
        )
    )
    if cached is None:
        raise ImportItemNotFound(f"No cached import item for task_cd={task_cd!r}")

    payload: dict[str, object] = {
        "tin": branch.tin,
        "bhfId": branch.bhf_id,
        "cmcKey": branch.cmc_key,
        "taskCd": cached.task_cd,
        "dclDe": cached.dcl_de,
        "itemSeq": cached.item_seq,
        "hsCd": cached.hs_cd,
        "itemClCd": item_cls_cd,
        "itemCd": item_cd,
        "imptItemsttsCd": decision_cd,
    }
    await outbox.enqueue(db, ENDPOINT, branch.id, payload)

    cached.item_cls_cd = item_cls_cd
    cached.item_cd = item_cd
    cached.impt_item_stts_cd = decision_cd
    return cached
