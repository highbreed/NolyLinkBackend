"""Scheduled jobs calling KRA "Get" endpoints incrementally via lastReqDt (spec §9, §11).

Celery tasks are synchronous entry points; each wraps an async implementation via
asyncio.run() rather than mixing async DB/HTTP code with Celery's own concurrency model.
All five pollers (codes, import items, purchase-sales, stock moves, notices) share the same
"for every ACTIVE branch, refresh, commit or roll back on KraError" shape — factored into
_refresh_for_all_branches so each task is just its resource's refresh function plus a name.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Branch
from app.db.session import AsyncSessionLocal
from app.services import (
    code_cache_service,
    import_item_service,
    notice_service,
    purchase_service,
    stock_service,
)
from app.services.kra_adapter import KraAdapter, KraError
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

RefreshFn = Callable[[AsyncSession, KraAdapter, Branch], Awaitable[None]]


async def _refresh_for_all_branches(
    refresh_fn: RefreshFn,
    resource_name: str,
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
) -> None:
    async with session_factory() as db:
        branches = (
            (await db.execute(select(Branch).where(Branch.status == "ACTIVE"))).scalars().all()
        )
        for branch in branches:
            kra = KraAdapter(db=db)
            try:
                await refresh_fn(db, kra, branch)
                await db.commit()
            except KraError:
                await db.rollback()
                logger.exception(
                    "%s refresh failed for branch_code=%s; last_req_dt not advanced, next"
                    " run retries the same window",
                    resource_name,
                    branch.branch_code,
                )
            finally:
                await kra.aclose()


async def _refresh_code_lists_for_all_branches(
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
) -> None:
    await _refresh_for_all_branches(code_cache_service.refresh_code_list, "codes", session_factory)


async def _refresh_import_items_for_all_branches(
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
) -> None:
    await _refresh_for_all_branches(
        import_item_service.refresh_import_items, "import_items", session_factory
    )


async def _refresh_purchase_sales_for_all_branches(
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
) -> None:
    await _refresh_for_all_branches(
        purchase_service.refresh_purchase_sales, "purchase_sales", session_factory
    )


async def _refresh_stock_moves_for_all_branches(
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
) -> None:
    await _refresh_for_all_branches(
        stock_service.refresh_stock_moves, "stock_moves", session_factory
    )


async def _refresh_notices_for_all_branches(
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
) -> None:
    await _refresh_for_all_branches(notice_service.refresh_notices, "notices", session_factory)


@celery_app.task(name="poller.refresh_code_lists")  # type: ignore[untyped-decorator]
def refresh_code_lists() -> None:
    asyncio.run(_refresh_code_lists_for_all_branches())


@celery_app.task(name="poller.refresh_import_items")  # type: ignore[untyped-decorator]
def refresh_import_items() -> None:
    asyncio.run(_refresh_import_items_for_all_branches())


@celery_app.task(name="poller.refresh_purchase_sales")  # type: ignore[untyped-decorator]
def refresh_purchase_sales() -> None:
    asyncio.run(_refresh_purchase_sales_for_all_branches())


@celery_app.task(name="poller.refresh_stock_moves")  # type: ignore[untyped-decorator]
def refresh_stock_moves() -> None:
    asyncio.run(_refresh_stock_moves_for_all_branches())


@celery_app.task(name="poller.refresh_notices")  # type: ignore[untyped-decorator]
def refresh_notices() -> None:
    asyncio.run(_refresh_notices_for_all_branches())
