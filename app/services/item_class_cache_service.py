"""Cache KRA's item-classification list (spec §6.3) via incremental /selectItemClsList sync —
mirrors app/services/code_cache_service.py's shape, but item classes have no cd_cls of their
own (see KraItemClass's docstring), so this is a parallel module rather than reusing that
one's (cd_cls, cd) keying.
"""

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Branch, KraItemClass
from app.schemas.kra.item import ItemClsSearchReq
from app.services import sync_framework
from app.services.kra_adapter import KraAdapter

RESOURCE = "item_classes"


async def refresh_item_classes(db: AsyncSession, kra: KraAdapter, branch: Branch) -> None:
    """Incremental sync via /selectItemClsList. On any KraError this simply doesn't advance
    last_req_dt, so the next run retries the same window — same rule as refresh_code_list.
    """
    last_req_dt = await sync_framework.get_last_req_dt(db, branch.tin, branch.bhf_id, RESOURCE)
    req = ItemClsSearchReq(
        tin=branch.tin, bhfId=branch.bhf_id, cmcKey=branch.cmc_key, lastReqDt=last_req_dt
    )
    res = await kra.get_item_class_list(req, branch_id=branch.id)

    if res.data is not None:
        for item_cls in res.data.itemClsList:
            await _upsert_item_class(
                db,
                item_cls_cd=item_cls.itemClsCd,
                item_cls_nm=item_cls.itemClsNm,
                item_cls_lvl=item_cls.itemClsLvl,
                tax_ty_cd=item_cls.taxTyCd,
                mjr_tg_yn=item_cls.mjrTgYn,
                use_yn=item_cls.useYn,
            )

    await sync_framework.advance_last_req_dt(db, branch.tin, branch.bhf_id, RESOURCE, res.resultDt)


async def _upsert_item_class(
    db: AsyncSession,
    item_cls_cd: str,
    item_cls_nm: str | None,
    item_cls_lvl: str | None,
    tax_ty_cd: str | None,
    mjr_tg_yn: str | None,
    use_yn: str | None,
) -> None:
    stmt = pg_insert(KraItemClass).values(
        item_cls_cd=item_cls_cd,
        item_cls_nm=item_cls_nm,
        item_cls_lvl=item_cls_lvl,
        tax_ty_cd=tax_ty_cd,
        mjr_tg_yn=mjr_tg_yn,
        use_yn=use_yn,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[KraItemClass.item_cls_cd],
        set_={
            "item_cls_nm": stmt.excluded.item_cls_nm,
            "item_cls_lvl": stmt.excluded.item_cls_lvl,
            "tax_ty_cd": stmt.excluded.tax_ty_cd,
            "mjr_tg_yn": stmt.excluded.mjr_tg_yn,
            "use_yn": stmt.excluded.use_yn,
            "refreshed_at": func.now(),
        },
    )
    await db.execute(stmt)


async def get_item_classes(
    db: AsyncSession,
    level: str | None = None,
    prefix: str | None = None,
    q: str | None = None,
) -> list[KraItemClass]:
    """`level`/`prefix`/`q` are optional filters used by the admin browsing endpoint
    (app/api/v1/endpoints/admin_reference_data.py) to page through the ~1000-row UNSPSC-style
    hierarchy — `prefix` matches item_cls_cd's leading digits (e.g. "50" for every code under
    the "Food and Beverage Products" segment), `q` is a case-insensitive substring match
    against item_cls_nm. The client-facing item_classes.py endpoint never passes any of these,
    so this stays a plain full listing there.
    """
    stmt = select(KraItemClass)
    if level:
        stmt = stmt.where(KraItemClass.item_cls_lvl == level)
    if prefix:
        stmt = stmt.where(KraItemClass.item_cls_cd.startswith(prefix))
    if q:
        stmt = stmt.where(func.lower(KraItemClass.item_cls_nm).like(f"%{q.casefold()}%"))
    result = await db.execute(stmt.order_by(KraItemClass.item_cls_cd))
    return list(result.scalars().all())
