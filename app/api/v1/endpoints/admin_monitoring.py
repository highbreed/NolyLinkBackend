"""Real backend for the admin portal's dashboard tiles, KRA-call timeseries chart, outbox
browser, and sync-lag view — previously all served by an in-browser mock (no endpoint existed
at all). Matches the frontend's existing MonitoringSummary/KraCallTimeseriesPoint/
OutboxItem/SyncStatusRow TS contracts field-for-field (the mock's shapes, not this file's own
invention) so the frontend swap is a pure URL change, no type changes.

All metric computation lives in app/services/metrics_service.py, reused as-is here and by the
alert-rule preview/evaluator — this file is purely the HTTP shape around those functions, plus
the outbox CRUD-ish actions (retry/cancel) admin_dashboard.py's call-log viewer doesn't need.
"""

import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_auth import require_admin
from app.db.models import Branch, Customer, KraCallLog, Outbox, VscuCallLog
from app.db.session import get_db
from app.schemas.page import Page
from app.services import metrics_service

router = APIRouter(dependencies=[Depends(require_admin)])

# Defaults not specified anywhere else — this gateway's own operational thresholds, not a KRA
# setting. Kept as module constants rather than new Settings fields since nothing else needs
# to configure them yet; promote to Settings if that changes.
STALE_BRANCH_THRESHOLD_HOURS = 24


class MonitoringSummaryOut(BaseModel):
    kra_call_success_rate_1h: float | None
    kra_call_success_rate_24h: float | None
    outbox_counts: dict[str, int]
    stale_branch_count: int
    blocked_customer_count: int
    worst_sync_offender: "SyncStatusRowOut | None"


class SyncStatusRowOut(BaseModel):
    branch_code: str
    resource: str
    last_req_dt: str
    updated_at: datetime
    lag_seconds: float


@router.get("/summary", response_model=MonitoringSummaryOut)
async def get_summary(db: AsyncSession = Depends(get_db)) -> MonitoringSummaryOut:
    rate_1h = await metrics_service.kra_call_success_rate(db, hours=1)
    rate_24h = await metrics_service.kra_call_success_rate(db, hours=24)
    counts = await metrics_service.outbox_counts(db)
    stale = await metrics_service.stale_branch_count(db, STALE_BRANCH_THRESHOLD_HOURS)
    blocked = await metrics_service.blocked_customer_count(db)
    worst = await metrics_service.worst_sync_offender(db)

    return MonitoringSummaryOut(
        kra_call_success_rate_1h=rate_1h,
        kra_call_success_rate_24h=rate_24h,
        outbox_counts=counts.as_dict(),
        stale_branch_count=stale,
        blocked_customer_count=blocked,
        worst_sync_offender=(
            SyncStatusRowOut(
                branch_code=worst.branch_code,
                resource=worst.resource,
                last_req_dt=worst.last_req_dt,
                updated_at=worst.updated_at,
                lag_seconds=worst.lag_seconds,
            )
            if worst is not None
            else None
        ),
    )


class KraCallTimeseriesPointOut(BaseModel):
    bucket_start: datetime
    counts_by_result_cd: dict[str, int]


@router.get("/kra-calls/timeseries", response_model=list[KraCallTimeseriesPointOut])
async def get_kra_calls_timeseries(
    range_hours: int = Query(default=24, ge=1, le=24 * 30),
    bucket_minutes: int = Query(default=60, ge=1, le=24 * 60),
    db: AsyncSession = Depends(get_db),
) -> list[KraCallTimeseriesPointOut]:
    since = datetime.now(UTC) - timedelta(hours=range_hours)
    bucket_seconds = bucket_minutes * 60

    kra_rows = (
        await db.execute(
            select(KraCallLog.created_at, KraCallLog.result_cd).where(
                KraCallLog.created_at >= since
            )
        )
    ).all()
    vscu_rows = (
        await db.execute(
            select(VscuCallLog.created_at, VscuCallLog.result_cd).where(
                VscuCallLog.created_at >= since
            )
        )
    ).all()

    buckets: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for created_at, result_cd in [*kra_rows, *vscu_rows]:
        bucket_key = int(created_at.timestamp() // bucket_seconds)
        buckets[bucket_key][result_cd or "UNKNOWN"] += 1

    return [
        KraCallTimeseriesPointOut(
            bucket_start=datetime.fromtimestamp(bucket_key * bucket_seconds, tz=UTC),
            counts_by_result_cd=dict(counts),
        )
        for bucket_key, counts in sorted(buckets.items())
    ]


class OutboxItemOut(BaseModel):
    id: uuid.UUID
    endpoint: str
    branch_code: str | None
    customer_id: uuid.UUID | None
    customer_name: str | None
    status: str
    attempts: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime


async def _branch_and_customer_lookup(
    db: AsyncSession,
) -> tuple[dict[uuid.UUID, Branch], dict[uuid.UUID, Customer]]:
    branches_by_id = {b.id: b for b in (await db.execute(select(Branch))).scalars().all()}
    customer_by_branch_id: dict[uuid.UUID, Customer] = {}
    for customer in (await db.execute(select(Customer))).scalars().all():
        for branch_id in customer.allowed_branch_ids:
            customer_by_branch_id[branch_id] = customer
    return branches_by_id, customer_by_branch_id


def _to_outbox_out(
    row: Outbox,
    branches_by_id: dict[uuid.UUID, Branch],
    customer_by_branch_id: dict[uuid.UUID, Customer],
) -> OutboxItemOut:
    branch = branches_by_id.get(row.branch_id) if row.branch_id else None
    customer = customer_by_branch_id.get(row.branch_id) if row.branch_id else None
    return OutboxItemOut(
        id=row.id,
        endpoint=row.endpoint,
        branch_code=branch.branch_code if branch else None,
        customer_id=customer.id if customer else None,
        customer_name=customer.name if customer else None,
        status=row.status,
        attempts=row.attempts,
        last_error=row.last_error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("/outbox", response_model=Page[OutboxItemOut])
async def list_outbox(
    status: str | None = None,
    branch_code: str | None = None,
    customer_id: uuid.UUID | None = None,
    q: str | None = Query(default=None, description="Free-text match on endpoint/branch/customer"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, le=200, ge=1),
    db: AsyncSession = Depends(get_db),
) -> Page[OutboxItemOut]:
    branches_by_id, customer_by_branch_id = await _branch_and_customer_lookup(db)

    branch_id: uuid.UUID | None = None
    if branch_code is not None:
        branch = next((b for b in branches_by_id.values() if b.branch_code == branch_code), None)
        if branch is None:
            raise HTTPException(status_code=404, detail="No branch with that branch_code")
        branch_id = branch.id

    stmt = select(Outbox)
    if status is not None:
        stmt = stmt.where(Outbox.status == status)
    if branch_id is not None:
        stmt = stmt.where(Outbox.branch_id == branch_id)
    rows = (await db.execute(stmt.order_by(Outbox.created_at.desc()))).scalars().all()

    if customer_id is not None:
        rows = [
            r
            for r in rows
            if r.branch_id is not None
            and customer_by_branch_id.get(r.branch_id) is not None
            and customer_by_branch_id[r.branch_id].id == customer_id
        ]

    if q:
        needle = q.lower()

        def _matches(r: Outbox) -> bool:
            b = branches_by_id.get(r.branch_id) if r.branch_id else None
            c = customer_by_branch_id.get(r.branch_id) if r.branch_id else None
            haystack = " ".join(
                filter(None, [r.endpoint, b.branch_code if b else None, c.name if c else None])
            )
            return needle in haystack.lower()

        rows = [r for r in rows if _matches(r)]

    total = len(rows)
    start = (page - 1) * page_size
    page_rows = rows[start : start + page_size]

    return Page[OutboxItemOut](
        items=[_to_outbox_out(r, branches_by_id, customer_by_branch_id) for r in page_rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/outbox/{outbox_id}", response_model=OutboxItemOut)
async def get_outbox_item(outbox_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> OutboxItemOut:
    row = await db.get(Outbox, outbox_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No outbox row with that id")
    branches_by_id, customer_by_branch_id = await _branch_and_customer_lookup(db)
    return _to_outbox_out(row, branches_by_id, customer_by_branch_id)


@router.post("/outbox/{outbox_id}/retry", response_model=OutboxItemOut)
async def retry_outbox_item(
    outbox_id: uuid.UUID,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> OutboxItemOut:
    """Resets attempts and status=PENDING so outbox_dispatcher's next tick picks it up
    immediately instead of waiting out its remaining backoff schedule (spec §14).
    """
    row = await db.get(Outbox, outbox_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No outbox row with that id")
    row.status = "PENDING"
    row.attempts = 0
    row.last_error = None
    await db.commit()
    await db.refresh(row)
    branches_by_id, customer_by_branch_id = await _branch_and_customer_lookup(db)
    return _to_outbox_out(row, branches_by_id, customer_by_branch_id)


@router.post("/outbox/{outbox_id}/cancel", response_model=OutboxItemOut)
async def cancel_outbox_item(
    outbox_id: uuid.UUID,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> OutboxItemOut:
    """status=CANCELLED is a new value (no migration needed, Outbox.status is a plain string)
    — outbox_dispatcher's _is_eligible only ever selects PENDING/RETRYING, so a cancelled row
    is never picked up again. The client needs to resubmit correctly instead; this just stops
    the retry loop on a row that's never going to succeed as-is.
    """
    row = await db.get(Outbox, outbox_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No outbox row with that id")
    row.status = "CANCELLED"
    await db.commit()
    await db.refresh(row)
    branches_by_id, customer_by_branch_id = await _branch_and_customer_lookup(db)
    return _to_outbox_out(row, branches_by_id, customer_by_branch_id)


@router.get("/sync-status", response_model=list[SyncStatusRowOut])
async def get_sync_status(
    branch_code: str | None = None, db: AsyncSession = Depends(get_db)
) -> list[SyncStatusRowOut]:
    rows = await metrics_service.sync_status(db, branch_code=branch_code)
    return [
        SyncStatusRowOut(
            branch_code=r.branch_code,
            resource=r.resource,
            last_req_dt=r.last_req_dt,
            updated_at=r.updated_at,
            lag_seconds=r.lag_seconds,
        )
        for r in rows
    ]
