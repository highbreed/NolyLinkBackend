"""Tests for app/services/metrics_service.py — the single implementation of every
dashboard/alert metric, reused by admin_monitoring.py and (later) the alert-rule evaluator.
"""

import uuid
from datetime import UTC, datetime, timedelta

from app.db.models import Branch, Customer, KraCallLog, KraSyncState, Outbox, Subscription, VscuCallLog
from app.services import metrics_service


def _tin() -> str:
    return f"A{uuid.uuid4().int % 10**9:09d}Z"


async def test_kra_call_success_rate_mixes_both_protocols(db_session):
    branch = Branch(branch_code="BR-METRICS-1", tin=_tin(), bhf_id="00", cmc_key="c" * 10)
    db_session.add(branch)
    await db_session.flush()

    db_session.add_all(
        [
            KraCallLog(endpoint="/e", request_hash="h", result_cd="000"),
            KraCallLog(endpoint="/e", request_hash="h", result_cd="999"),
            VscuCallLog(endpoint="/e", request_hash="h", result_cd="001"),
            VscuCallLog(endpoint="/e", request_hash="h", result_cd="990"),
        ]
    )
    await db_session.flush()

    rate = await metrics_service.kra_call_success_rate(db_session, hours=24)
    assert rate == 0.5


async def test_kra_call_success_rate_none_when_no_calls(db_session):
    rate = await metrics_service.kra_call_success_rate(db_session, hours=1)
    assert rate is None


async def test_kra_call_success_rate_excludes_calls_outside_window(db_session):
    old = KraCallLog(endpoint="/e", request_hash="h", result_cd="999")
    db_session.add(old)
    await db_session.flush()
    old.created_at = datetime.now(UTC) - timedelta(hours=2)
    db_session.add(KraCallLog(endpoint="/e", request_hash="h", result_cd="000"))
    await db_session.flush()

    rate = await metrics_service.kra_call_success_rate(db_session, hours=1)
    assert rate == 1.0


async def test_outbox_counts_groups_by_status(db_session):
    db_session.add_all(
        [
            Outbox(endpoint="/e", payload={}, status="PENDING"),
            Outbox(endpoint="/e", payload={}, status="PENDING"),
            Outbox(endpoint="/e", payload={}, status="FAILED"),
            Outbox(endpoint="/e", payload={}, status="CANCELLED"),
        ]
    )
    await db_session.flush()

    counts = await metrics_service.outbox_counts(db_session)
    assert counts.as_dict() == {"PENDING": 2, "RETRYING": 0, "SENT": 0, "FAILED": 1, "CANCELLED": 1}


async def test_sync_status_computes_lag_and_sorts_worst_first(db_session):
    tin = _tin()
    branch = Branch(branch_code="BR-METRICS-2", tin=tin, bhf_id="00", cmc_key="c" * 10)
    db_session.add(branch)
    await db_session.flush()

    stale = KraSyncState(tin=tin, bhf_id="00", resource="codes", last_req_dt="20260101000000")
    fresh = KraSyncState(tin=tin, bhf_id="00", resource="notices", last_req_dt="20260101000000")
    db_session.add_all([stale, fresh])
    await db_session.flush()
    stale.updated_at = datetime.now(UTC) - timedelta(hours=10)
    fresh.updated_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.flush()

    rows = await metrics_service.sync_status(db_session)
    assert [r.resource for r in rows] == ["codes", "notices"]
    assert rows[0].lag_seconds > rows[1].lag_seconds

    worst = await metrics_service.worst_sync_offender(db_session)
    assert worst is not None
    assert worst.resource == "codes"


async def test_sync_status_filters_by_branch_code(db_session):
    tin_a, tin_b = _tin(), _tin()
    branch_a = Branch(branch_code="BR-METRICS-A", tin=tin_a, bhf_id="00", cmc_key="c" * 10)
    branch_b = Branch(branch_code="BR-METRICS-B", tin=tin_b, bhf_id="00", cmc_key="c" * 10)
    db_session.add_all([branch_a, branch_b])
    await db_session.flush()
    db_session.add_all(
        [
            KraSyncState(tin=tin_a, bhf_id="00", resource="codes", last_req_dt="x"),
            KraSyncState(tin=tin_b, bhf_id="00", resource="codes", last_req_dt="x"),
        ]
    )
    await db_session.flush()

    rows = await metrics_service.sync_status(db_session, branch_code="BR-METRICS-A")
    assert [r.branch_code for r in rows] == ["BR-METRICS-A"]


async def test_stale_branch_count_counts_missing_and_old_sync_rows(db_session):
    tin_missing, tin_old, tin_fresh = _tin(), _tin(), _tin()
    db_session.add_all(
        [
            Branch(branch_code="BR-STALE-MISSING", tin=tin_missing, bhf_id="00", cmc_key="c" * 10),
            Branch(branch_code="BR-STALE-OLD", tin=tin_old, bhf_id="00", cmc_key="c" * 10),
            Branch(branch_code="BR-STALE-FRESH", tin=tin_fresh, bhf_id="00", cmc_key="c" * 10),
            Branch(
                branch_code="BR-STALE-INACTIVE",
                tin=_tin(),
                bhf_id="00",
                cmc_key="c" * 10,
                status="INACTIVE",
            ),
        ]
    )
    await db_session.flush()
    old_row = KraSyncState(tin=tin_old, bhf_id="00", resource="codes", last_req_dt="x")
    fresh_row = KraSyncState(tin=tin_fresh, bhf_id="00", resource="codes", last_req_dt="x")
    db_session.add_all([old_row, fresh_row])
    await db_session.flush()
    old_row.updated_at = datetime.now(UTC) - timedelta(hours=48)
    await db_session.flush()

    stale = await metrics_service.stale_branch_count(db_session, threshold_hours=24)
    # Missing + old = 2. The fresh one isn't stale. The INACTIVE branch is never counted.
    assert stale == 2


async def test_blocked_customer_count_matches_frontend_blocked_bucket(db_session):
    customers = [Customer(name=f"Metrics Co {i}", allowed_branch_ids=[]) for i in range(5)]
    db_session.add_all(customers)
    await db_session.flush()

    def sub(customer, status, period_end_delta_days, grace_days=None):
        return Subscription(
            customer_id=customer.id,
            plan_name="STANDARD",
            status=status,
            current_period_end=datetime.now(UTC) + timedelta(days=period_end_delta_days),
            grace_period_days=grace_days,
        )

    db_session.add_all(
        [
            # Active, within period — not blocked.
            sub(customers[0], "ACTIVE", 10),
            # Active, period ended but within default grace — not blocked (that's "GRACE").
            sub(customers[1], "ACTIVE", -1, grace_days=7),
            # Active, period + grace both passed — blocked.
            sub(customers[2], "ACTIVE", -10, grace_days=7),
            # Explicitly SUSPENDED — its own bucket, never counted as "blocked" here.
            sub(customers[3], "SUSPENDED", -10, grace_days=7),
            # Explicitly CANCELLED — likewise never counted.
            sub(customers[4], "CANCELLED", -10, grace_days=7),
        ]
    )
    await db_session.flush()

    blocked = await metrics_service.blocked_customer_count(db_session)
    assert blocked == 1
