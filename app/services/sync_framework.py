"""Generic lastReqDt incremental-sync framework (spec §11), reused by every poller from
Phase 5 onward (codes now; item classes, notices, items, branches, import items,
purchase-sales, and stock moves in later phases).

Rule (spec §2.2 rule 3 / §11): advance last_req_dt to the KRA server's own resultDt, never the
client clock, and only on success — a failed sync must retry the same window next run.
"""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import KraSyncState


async def get_last_req_dt(db: AsyncSession, tin: str, bhf_id: str, resource: str) -> str:
    """The initial value for a resource never synced before isn't specified anywhere in the
    source spec — KRA_SYNC_INITIAL_LAST_REQ_DT is a config flag rather than a hardcoded
    guess, so it can be corrected once sandbox behavior is confirmed.
    """
    state = await db.scalar(
        select(KraSyncState).where(
            KraSyncState.tin == tin,
            KraSyncState.bhf_id == bhf_id,
            KraSyncState.resource == resource,
        )
    )
    if state is not None:
        return state.last_req_dt
    return get_settings().kra_sync_initial_last_req_dt


async def advance_last_req_dt(
    db: AsyncSession, tin: str, bhf_id: str, resource: str, result_dt: str
) -> None:
    stmt = pg_insert(KraSyncState).values(
        tin=tin, bhf_id=bhf_id, resource=resource, last_req_dt=result_dt
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[KraSyncState.tin, KraSyncState.bhf_id, KraSyncState.resource],
        set_={"last_req_dt": stmt.excluded.last_req_dt},
    )
    await db.execute(stmt)
    await db.flush()
