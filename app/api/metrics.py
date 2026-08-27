"""GET /metrics — Prometheus scrape endpoint. Unversioned and unauthenticated, matching
standard Prometheus convention (the scraper itself, not an API client, hits this).

outbox_queue_depth and kra_sync_lag_seconds are DB-backed gauges refreshed here at scrape
time rather than on a timer, so they're never stale between scrapes.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.core.metrics import kra_sync_lag_seconds, outbox_queue_depth
from app.db.models import KraSyncState, Outbox
from app.db.session import get_db

router = APIRouter()


async def _refresh_outbox_depth(db: AsyncSession) -> None:
    result = await db.execute(select(Outbox.status, func.count()).group_by(Outbox.status))
    counts: dict[str, int] = dict(result.all())  # type: ignore[arg-type]
    for status in ("PENDING", "RETRYING", "SENT", "FAILED"):
        outbox_queue_depth.labels(status=status).set(counts.get(status, 0))


async def _refresh_sync_lag(db: AsyncSession) -> None:
    result = await db.execute(
        select(KraSyncState.resource, func.max(KraSyncState.updated_at)).group_by(
            KraSyncState.resource
        )
    )
    now = datetime.now(UTC)
    for resource, last_updated in result.all():
        kra_sync_lag_seconds.labels(resource=resource).set((now - last_updated).total_seconds())


@router.get("/metrics")
async def metrics(db: AsyncSession = Depends(get_db)) -> Response:
    await _refresh_outbox_depth(db)
    await _refresh_sync_lag(db)
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
