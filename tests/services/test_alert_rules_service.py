"""Tests for app/services/alert_rules_service.py — CRUD, condition evaluation, and the
kra_call_failure_rate historical backtest.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.db.models import Branch, KraCallLog
from app.services import alert_rules_service


def _tin() -> str:
    return f"A{uuid.uuid4().int % 10**9:09d}Z"


@pytest.mark.parametrize(
    "condition,value,threshold,expected",
    [
        (">", 5, 3, True),
        (">", 3, 5, False),
        (">=", 5, 5, True),
        ("==", 5, 5, True),
        ("==", 5, 5.01, False),
        ("<=", 3, 5, True),
        ("<=", 5, 3, False),
    ],
)
def test_evaluate_condition(condition, value, threshold, expected):
    assert alert_rules_service.evaluate_condition(value, condition, threshold) is expected


def test_evaluate_condition_rejects_unknown_operator():
    with pytest.raises(ValueError):
        alert_rules_service.evaluate_condition(1, "!=", 1)


async def test_create_rejects_unknown_metric(db_session):
    with pytest.raises(alert_rules_service.UnknownMetricError):
        await alert_rules_service.create_alert_rule(
            db_session,
            name="bad",
            metric="not_a_real_metric",
            scope_customer_id=None,
            scope_branch_code=None,
            condition=">",
            threshold=Decimal("1"),
            channel="webhook",
            channel_target="https://example.com",
            enabled=True,
        )


async def test_create_list_and_update_round_trip(db_session):
    rule = await alert_rules_service.create_alert_rule(
        db_session,
        name="High failure rate",
        metric="kra_call_failure_rate",
        scope_customer_id=None,
        scope_branch_code=None,
        condition=">",
        threshold=Decimal("0.1"),
        channel="webhook",
        channel_target="https://example.com/hook",
        enabled=True,
    )
    all_rules = await alert_rules_service.list_alert_rules(db_session)
    assert [r.id for r in all_rules] == [rule.id]

    updated = await alert_rules_service.update_alert_rule(
        db_session, rule.id, enabled=False, threshold=Decimal("0.2")
    )
    assert updated.enabled is False
    assert updated.threshold == Decimal("0.2")
    assert updated.name == "High failure rate"  # untouched fields stay as-is

    enabled_rules = await alert_rules_service.list_enabled_alert_rules(db_session)
    assert enabled_rules == []


async def test_update_can_clear_scope_to_null(db_session):
    rule = await alert_rules_service.create_alert_rule(
        db_session,
        name="Scoped",
        metric="outbox_failed_count",
        scope_customer_id=None,
        scope_branch_code="BR-1",
        condition=">",
        threshold=Decimal("0"),
        channel="webhook",
        channel_target="https://example.com",
        enabled=True,
    )
    assert rule.scope_branch_code == "BR-1"

    updated = await alert_rules_service.update_alert_rule(
        db_session, rule.id, scope_branch_code=None
    )
    assert updated.scope_branch_code is None


async def test_update_unknown_rule_raises(db_session):
    with pytest.raises(alert_rules_service.AlertRuleNotFoundError):
        await alert_rules_service.update_alert_rule(db_session, uuid.uuid4(), enabled=False)


async def test_backtest_counts_buckets_crossing_threshold(db_session):
    branch = Branch(branch_code="BR-BACKTEST", tin=_tin(), bhf_id="00", cmc_key="c" * 10)
    db_session.add(branch)
    await db_session.flush()

    now = datetime.now(UTC)
    rows = [
        # Bucket 1 (2 hours ago): 2 failures, 0 success -> failure rate 1.0, fires at >0.5.
        KraCallLog(branch_id=branch.id, endpoint="/e", request_hash="h", result_cd="999"),
        KraCallLog(branch_id=branch.id, endpoint="/e", request_hash="h", result_cd="999"),
        # Bucket 2 (1 hour ago): 1 failure, 1 success -> failure rate 0.5, does not fire at >0.5.
        KraCallLog(branch_id=branch.id, endpoint="/e", request_hash="h", result_cd="999"),
        KraCallLog(branch_id=branch.id, endpoint="/e", request_hash="h", result_cd="000"),
    ]
    db_session.add_all(rows)
    await db_session.flush()
    rows[0].created_at = now - timedelta(hours=2)
    rows[1].created_at = now - timedelta(hours=2)
    rows[2].created_at = now - timedelta(hours=1)
    rows[3].created_at = now - timedelta(hours=1)
    await db_session.flush()

    fired = await alert_rules_service.backtest_kra_call_failure_rate(
        db_session, ">", 0.5, scope_branch_code="BR-BACKTEST"
    )
    assert fired == 1


async def test_backtest_unknown_branch_returns_zero(db_session):
    fired = await alert_rules_service.backtest_kra_call_failure_rate(
        db_session, ">", 0.0, scope_branch_code="no-such-branch"
    )
    assert fired == 0
