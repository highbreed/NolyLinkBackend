"""Shared metric computations — the single implementation of "what does kra_call_failure_rate
(or any other dashboard/alert metric) mean right now", reused by admin_monitoring.py's
dashboard/timeseries/sync-status endpoints AND the alert-rule preview/evaluator, so there is
never a second, divergently-defined copy of any of these.

Success convention for KraCallLog/VscuCallLog (result_cd in ("000", "001")) matches
app/services/kra_adapter.py's own definition exactly — "001" (no search result) is an empty
result, not an error (spec).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Branch, KraCallLog, KraSyncState, Outbox, Subscription, VscuCallLog

SUCCESS_RESULT_CODES = ("000", "001")


async def kra_call_success_rate(db: AsyncSession, hours: int) -> float | None:
    """Fraction (0.0-1.0) of KraCallLog + VscuCallLog rows in the last `hours` whose
    result_cd indicates success. None if there were no calls at all in that window (there is
    no meaningful rate to report, not a 0% or 100% one).
    """
    since = datetime.now(UTC) - timedelta(hours=hours)
    kra_codes = (
        await db.execute(select(KraCallLog.result_cd).where(KraCallLog.created_at >= since))
    ).scalars().all()
    vscu_codes = (
        await db.execute(select(VscuCallLog.result_cd).where(VscuCallLog.created_at >= since))
    ).scalars().all()
    all_codes = list(kra_codes) + list(vscu_codes)
    if not all_codes:
        return None
    successes = sum(1 for code in all_codes if code in SUCCESS_RESULT_CODES)
    return successes / len(all_codes)


@dataclass
class OutboxCounts:
    pending: int = 0
    retrying: int = 0
    sent: int = 0
    failed: int = 0
    cancelled: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "PENDING": self.pending,
            "RETRYING": self.retrying,
            "SENT": self.sent,
            "FAILED": self.failed,
            "CANCELLED": self.cancelled,
        }


async def outbox_counts(db: AsyncSession) -> OutboxCounts:
    rows = (await db.execute(select(Outbox.status))).scalars().all()
    counts = OutboxCounts()
    for status in rows:
        if status == "PENDING":
            counts.pending += 1
        elif status == "RETRYING":
            counts.retrying += 1
        elif status == "SENT":
            counts.sent += 1
        elif status == "FAILED":
            counts.failed += 1
        elif status == "CANCELLED":
            counts.cancelled += 1
    return counts


@dataclass
class SyncStatusRow:
    branch_code: str
    resource: str
    last_req_dt: str
    updated_at: datetime
    lag_seconds: float


async def sync_status(db: AsyncSession, branch_code: str | None = None) -> list[SyncStatusRow]:
    """One row per (branch, resource) KraSyncState tracks — lag_seconds is wall-clock time
    since our own last successful sync of that resource, not KRA's resultDt (which isn't
    wall-clock-comparable across calls, see KraSyncState's own docstring).
    """
    branches_by_tin_bhf = {
        (b.tin, b.bhf_id): b.branch_code
        for b in (await db.execute(select(Branch))).scalars().all()
        if branch_code is None or b.branch_code == branch_code
    }
    now = datetime.now(UTC)
    rows = (await db.execute(select(KraSyncState))).scalars().all()
    result = [
        SyncStatusRow(
            branch_code=branches_by_tin_bhf[(row.tin, row.bhf_id)],
            resource=row.resource,
            last_req_dt=row.last_req_dt,
            updated_at=row.updated_at,
            lag_seconds=(now - row.updated_at).total_seconds(),
        )
        for row in rows
        if (row.tin, row.bhf_id) in branches_by_tin_bhf
    ]
    result.sort(key=lambda r: r.lag_seconds, reverse=True)
    return result


async def stale_branch_count(db: AsyncSession, threshold_hours: int) -> int:
    """Active branches with no KraSyncState row at all, or whose newest one is older than
    threshold_hours.
    """
    branches = (
        await db.execute(select(Branch).where(Branch.status == "ACTIVE"))
    ).scalars().all()
    sync_rows = (await db.execute(select(KraSyncState))).scalars().all()
    latest_by_tin_bhf: dict[tuple[str, str], datetime] = {}
    for row in sync_rows:
        key = (row.tin, row.bhf_id)
        if key not in latest_by_tin_bhf or row.updated_at > latest_by_tin_bhf[key]:
            latest_by_tin_bhf[key] = row.updated_at

    cutoff = datetime.now(UTC) - timedelta(hours=threshold_hours)
    stale = 0
    for branch in branches:
        latest = latest_by_tin_bhf.get((branch.tin, branch.bhf_id))
        if latest is None or latest < cutoff:
            stale += 1
    return stale


async def blocked_customer_count(db: AsyncSession) -> int:
    """Mirrors the frontend's own "BLOCKED" bucket exactly (src/lib/billingStatus.ts) and
    app/services/billing_service.py's enforce_active_subscription: a Subscription whose
    status is neither CANCELLED nor SUSPENDED (those are their own separate buckets/concepts),
    but current_period_end + grace_period_days has passed. A customer with no Subscription row
    at all is NOT counted — meaningless while BILLING_ENFORCEMENT_ENABLED is off/not backfilled.
    """
    settings = get_settings()
    now = datetime.now(UTC)
    subscriptions = (await db.execute(select(Subscription))).scalars().all()
    blocked = 0
    for sub in subscriptions:
        if sub.status in ("CANCELLED", "SUSPENDED"):
            continue
        grace_days = (
            sub.grace_period_days
            if sub.grace_period_days is not None
            else settings.billing_grace_period_days
        )
        if now > sub.current_period_end + timedelta(days=grace_days):
            blocked += 1
    return blocked


async def worst_sync_offender(db: AsyncSession) -> SyncStatusRow | None:
    rows = await sync_status(db)
    return rows[0] if rows else None
