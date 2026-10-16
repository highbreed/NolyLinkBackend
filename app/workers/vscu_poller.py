"""Scheduled jobs calling VSCU "Get"/select endpoints incrementally via lastReqDt — the VSCU
counterpart to app/workers/poller.py. Same "_refresh_for_all_branches" shape (for every
*VSCU* branch, refresh, commit or roll back on VSCUError), filtered to Branch.device_type ==
"VSCU" so this never touches an OSCU branch (which has no VscuDeviceState and would just fail
VSCUConfigurationError on every poll otherwise — filtering explicitly avoids that noise).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Branch
from app.db.session import AsyncSessionLocal
from app.integrations.vscu.client import VSCUClient
from app.integrations.vscu.exceptions import VSCUError
from app.services import (
    sync_framework,
    vscu_purchase_service,
    vscu_reference_data_service,
    vscu_stock_service,
)
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

RefreshFn = Callable[[AsyncSession, VSCUClient, Branch], Awaitable[None]]


async def _refresh_for_all_vscu_branches(
    refresh_fn: RefreshFn,
    resource_name: str,
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
) -> None:
    async with session_factory() as db:
        # A plain (id, branch_code) tuple, not a live ORM row: db.rollback() on one branch's
        # failure (below) expires every ORM object in the *whole* session — including
        # branches not yet processed, not just the failed one — so any later attribute touch
        # on a not-yet-refreshed Branch object would need a lazy-load requiring an active
        # greenlet context, raising MissingGreenlet outside one. Confirmed via this file's own
        # tests. Re-fetching fresh via db.get() per iteration (below) sidesteps that entirely;
        # app/workers/poller.py carries the identical latent bug in its OSCU equivalent, just
        # never exercised by its (single-test) suite — not fixed here, out of scope.
        rows = await db.execute(
            select(Branch.id, Branch.branch_code).where(
                Branch.status == "ACTIVE", Branch.device_type == "VSCU"
            )
        )
        branch_ids_and_codes = rows.all()

        for branch_id, branch_code in branch_ids_and_codes:
            branch = await db.get(Branch, branch_id)
            assert branch is not None  # can't vanish between the two queries within one session
            vscu = VSCUClient(db=db)
            try:
                await refresh_fn(db, vscu, branch)
                await db.commit()
            except VSCUError:
                await db.rollback()
                logger.exception(
                    "VSCU %s refresh failed for branch_code=%s; last_req_dt not advanced, "
                    "next run retries the same window",
                    resource_name,
                    branch_code,
                )
            finally:
                await vscu.aclose()


async def _refresh_codes_for_all_vscu_branches(
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
) -> None:
    async def _refresh(db: AsyncSession, vscu: VSCUClient, branch: Branch) -> None:
        await vscu_reference_data_service.VSCUReferenceDataService(db, vscu).sync_codes(branch)

    await _refresh_for_all_vscu_branches(_refresh, "codes", session_factory)


async def _refresh_item_classes_for_all_vscu_branches(
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
) -> None:
    resource = vscu_reference_data_service.ITEM_CLASSES_RESOURCE

    async def _refresh(db: AsyncSession, vscu: VSCUClient, branch: Branch) -> None:
        last_req_dt = await sync_framework.get_last_req_dt(db, branch.tin, branch.bhf_id, resource)
        await vscu_reference_data_service.VSCUReferenceDataService(db, vscu).select_item_classes(
            branch, last_req_dt=last_req_dt
        )

    await _refresh_for_all_vscu_branches(_refresh, resource, session_factory)


async def _refresh_notices_for_all_vscu_branches(
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
) -> None:
    async def _refresh(db: AsyncSession, vscu: VSCUClient, branch: Branch) -> None:
        await vscu_reference_data_service.VSCUReferenceDataService(db, vscu).sync_notices(branch)

    await _refresh_for_all_vscu_branches(_refresh, "notices", session_factory)


async def _refresh_import_items_for_all_vscu_branches(
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
) -> None:
    async def _refresh(db: AsyncSession, vscu: VSCUClient, branch: Branch) -> None:
        await vscu_reference_data_service.VSCUReferenceDataService(db, vscu).sync_import_items(
            branch
        )

    await _refresh_for_all_vscu_branches(_refresh, "import_items", session_factory)


async def _refresh_purchase_sales_for_all_vscu_branches(
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
) -> None:
    await _refresh_for_all_vscu_branches(
        vscu_purchase_service.sync_purchase_sales, "purchase_sales", session_factory
    )


async def _refresh_stock_moves_for_all_vscu_branches(
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
) -> None:
    await _refresh_for_all_vscu_branches(
        vscu_stock_service.sync_stock_moves, "stock_moves", session_factory
    )


@celery_app.task(name="vscu_poller.refresh_codes")  # type: ignore[untyped-decorator]
def refresh_codes() -> None:
    asyncio.run(_refresh_codes_for_all_vscu_branches())


@celery_app.task(name="vscu_poller.refresh_item_classes")  # type: ignore[untyped-decorator]
def refresh_item_classes() -> None:
    asyncio.run(_refresh_item_classes_for_all_vscu_branches())


@celery_app.task(name="vscu_poller.refresh_notices")  # type: ignore[untyped-decorator]
def refresh_notices() -> None:
    asyncio.run(_refresh_notices_for_all_vscu_branches())


@celery_app.task(name="vscu_poller.refresh_import_items")  # type: ignore[untyped-decorator]
def refresh_import_items() -> None:
    asyncio.run(_refresh_import_items_for_all_vscu_branches())


@celery_app.task(name="vscu_poller.refresh_purchase_sales")  # type: ignore[untyped-decorator]
def refresh_purchase_sales() -> None:
    asyncio.run(_refresh_purchase_sales_for_all_vscu_branches())


@celery_app.task(name="vscu_poller.refresh_stock_moves")  # type: ignore[untyped-decorator]
def refresh_stock_moves() -> None:
    asyncio.run(_refresh_stock_moves_for_all_vscu_branches())
