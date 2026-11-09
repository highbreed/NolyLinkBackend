"""Retention cleanup for tables that accumulate without bound otherwise.

client_call_log (app/db/models.py, written by app/core/audit.py) persists raw, redacted
request/response bodies on every single request — fiscal-PII-adjacent even after redaction,
so it is not kept forever. Deletes rows older than
Settings.client_call_log_retention_days, once a day (app/workers/celery_app.py's
beat_schedule).
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.db.models import ClientCallLog
from app.db.session import AsyncSessionLocal
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


async def _cleanup_client_call_log(
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
) -> int:
    cutoff = datetime.now(UTC) - timedelta(days=get_settings().client_call_log_retention_days)
    async with session_factory() as db:
        result = await db.execute(delete(ClientCallLog).where(ClientCallLog.created_at < cutoff))
        await db.commit()
        deleted = result.rowcount or 0  # type: ignore[attr-defined]
    if deleted:
        logger.info("Deleted %d client_call_log row(s) older than %s", deleted, cutoff)
    return deleted


@celery_app.task(name="cleanup.client_call_log")  # type: ignore[untyped-decorator]
def cleanup_client_call_log() -> int:
    return asyncio.run(_cleanup_client_call_log())
