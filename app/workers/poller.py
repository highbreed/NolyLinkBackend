"""Scheduled jobs calling KRA "Get" endpoints incrementally via lastReqDt (spec §9, §11).

Celery tasks are synchronous entry points; each wraps an async implementation via
asyncio.run() rather than mixing async DB/HTTP code with Celery's own concurrency model.
"""

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Branch
from app.db.session import AsyncSessionLocal
from app.services import code_cache_service
from app.services.kra_adapter import KraAdapter, KraError
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


async def _refresh_code_lists_for_all_branches(
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
) -> None:
    async with session_factory() as db:
        branches = (
            (await db.execute(select(Branch).where(Branch.status == "ACTIVE"))).scalars().all()
        )
        for branch in branches:
            kra = KraAdapter(db=db)
            try:
                await code_cache_service.refresh_code_list(db, kra, branch)
                await db.commit()
            except KraError:
                await db.rollback()
                logger.exception(
                    "Code list refresh failed for branch_code=%s; last_req_dt not advanced,"
                    " next run retries the same window",
                    branch.branch_code,
                )
            finally:
                await kra.aclose()


@celery_app.task(name="poller.refresh_code_lists")  # type: ignore[untyped-decorator]
def refresh_code_lists() -> None:
    asyncio.run(_refresh_code_lists_for_all_branches())
