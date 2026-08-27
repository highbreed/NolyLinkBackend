"""Retries failed/queued KRA sends from the outbox table with exponential backoff (spec §14).

Backoff schedule is 30s, 2m, 10m, 1h per the spec text, then the row is marked FAILED and
logged at ERROR ("alert") rather than retried forever. Not every KraError is retried:
  - KraValidationError/KraBusinessError/KraAuthError are permanent failures — a payload bug
    or a business-rule violation won't fix itself on retry, so these fail immediately rather
    than burning through the backoff schedule.
  - KraDuplicateError (994, "Overlapped data") is treated as success-equivalent per spec §10.
  - KraRateLimitError/KraTransientServerError/KraUnknownError are retried with backoff — the
    case the schedule above actually exists for.

Uses SELECT ... FOR UPDATE SKIP LOCKED (spec §19: "outbox/poller workers must use DB-level
locking to avoid double-processing") so multiple dispatcher instances can run concurrently.
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Outbox, Purchase, StockIOSubmission
from app.db.session import AsyncSessionLocal
from app.schemas.kra.import_item import ImportItemUpdateReq
from app.schemas.kra.purchase import TrnsPurchaseSaveReq
from app.schemas.kra.stock import StockIOSaveReq, StockMasterSaveReq
from app.services.kra_adapter import (
    KraAdapter,
    KraAuthError,
    KraBusinessError,
    KraDuplicateError,
    KraError,
    KraValidationError,
)
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

BACKOFF_SCHEDULE_SECONDS = [30, 120, 600, 3600]  # 30s, 2m, 10m, 1h (spec §14)
MAX_ATTEMPTS = len(BACKOFF_SCHEDULE_SECONDS) + 1  # one more failure after the last backoff step

ENDPOINT_HANDLERS: dict[str, tuple[type[BaseModel], str]] = {
    "/insertTrnsPurchase": (TrnsPurchaseSaveReq, "save_purchase_transaction"),
    "/insertStockIO": (StockIOSaveReq, "save_stock_io"),
    "/saveStockMaster": (StockMasterSaveReq, "save_stock_master"),
    "/updateImportItem": (ImportItemUpdateReq, "update_import_item"),
}

PERMANENT_FAILURE_EXCEPTIONS = (KraValidationError, KraBusinessError, KraAuthError)


def _is_eligible(row: Outbox, now: datetime) -> bool:
    if row.status not in ("PENDING", "RETRYING"):
        return False
    if row.attempts == 0:
        return True
    idx = min(row.attempts - 1, len(BACKOFF_SCHEDULE_SECONDS) - 1)
    delay = timedelta(seconds=BACKOFF_SCHEDULE_SECONDS[idx])
    return now >= row.updated_at + delay


async def _update_linked_business_record(
    db: AsyncSession, outbox_id: uuid.UUID, status: str
) -> None:
    purchase = await db.scalar(select(Purchase).where(Purchase.outbox_id == outbox_id))
    if purchase is not None:
        purchase.status = status
        return

    stock_io = await db.scalar(
        select(StockIOSubmission).where(StockIOSubmission.outbox_id == outbox_id)
    )
    if stock_io is not None:
        stock_io.status = status


async def _dispatch_one(db: AsyncSession, kra: KraAdapter, row: Outbox) -> None:
    schema, method_name = ENDPOINT_HANDLERS[row.endpoint]
    req = schema.model_validate(row.payload)
    method = getattr(kra, method_name)

    try:
        await method(req, branch_id=row.branch_id)
    except KraDuplicateError:
        row.status = "SENT"
        row.last_error = None
        await _update_linked_business_record(db, row.id, "SENT")
        return
    except PERMANENT_FAILURE_EXCEPTIONS as exc:
        row.status = "FAILED"
        row.attempts += 1
        row.last_error = f"{exc.result_cd}: {exc.result_msg}"
        logger.error(
            "Outbox row %s (%s) permanently failed (not retriable): %s",
            row.id,
            row.endpoint,
            row.last_error,
        )
        await _update_linked_business_record(db, row.id, "FAILED")
        return
    except KraError as exc:
        row.attempts += 1
        row.last_error = f"{exc.result_cd}: {exc.result_msg}"
        if row.attempts >= MAX_ATTEMPTS:
            row.status = "FAILED"
            logger.error(
                "Outbox row %s (%s) exhausted retries after %d attempts: %s",
                row.id,
                row.endpoint,
                row.attempts,
                row.last_error,
            )
        else:
            row.status = "RETRYING"
        await _update_linked_business_record(db, row.id, row.status)
        return

    row.status = "SENT"
    row.last_error = None
    await _update_linked_business_record(db, row.id, "SENT")


async def _dispatch_pending(
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
) -> None:
    now = datetime.now(UTC)
    async with session_factory() as db:
        stmt = (
            select(Outbox)
            .where(Outbox.status.in_(["PENDING", "RETRYING"]))
            .with_for_update(skip_locked=True)
        )
        candidates = (await db.execute(stmt)).scalars().all()
        kra = KraAdapter(db=db)
        try:
            for row in candidates:
                if not _is_eligible(row, now):
                    continue
                await _dispatch_one(db, kra, row)
                await db.commit()
        finally:
            await kra.aclose()


@celery_app.task(name="outbox_dispatcher.dispatch_pending")  # type: ignore[untyped-decorator]
def dispatch_pending() -> None:
    asyncio.run(_dispatch_pending())
