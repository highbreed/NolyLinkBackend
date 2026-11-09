"""CRUD for AlertRule plus the condition-evaluation + historical-backtest logic shared by the
preview endpoint and app/workers/alert_evaluator.py.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AlertRule, KraCallLog, VscuCallLog
from app.services.metrics_service import KNOWN_METRICS, SUCCESS_RESULT_CODES, branch_id_for_code

CONDITIONS = (">", ">=", "==", "<=")


class AlertRuleNotFoundError(Exception):
    pass


class UnknownMetricError(Exception):
    pass


def evaluate_condition(value: float, condition: str, threshold: float) -> bool:
    if condition == ">":
        return value > threshold
    if condition == ">=":
        return value >= threshold
    if condition == "==":
        return value == threshold
    if condition == "<=":
        return value <= threshold
    raise ValueError(f"Unknown condition: {condition!r}")


def _validate_metric(metric: str) -> None:
    if metric not in KNOWN_METRICS:
        raise UnknownMetricError(f"Unknown metric: {metric!r}")


async def create_alert_rule(
    db: AsyncSession,
    *,
    name: str,
    metric: str,
    scope_customer_id: uuid.UUID | None,
    scope_branch_code: str | None,
    condition: str,
    threshold: Decimal,
    channel: str,
    channel_target: str,
    enabled: bool,
) -> AlertRule:
    _validate_metric(metric)
    rule = AlertRule(
        name=name,
        metric=metric,
        scope_customer_id=scope_customer_id,
        scope_branch_code=scope_branch_code,
        condition=condition,
        threshold=threshold,
        channel=channel,
        channel_target=channel_target,
        enabled=enabled,
    )
    db.add(rule)
    await db.flush()
    return rule


async def list_alert_rules(db: AsyncSession) -> list[AlertRule]:
    return list((await db.execute(select(AlertRule).order_by(AlertRule.created_at.desc()))).scalars().all())


async def list_enabled_alert_rules(db: AsyncSession) -> list[AlertRule]:
    return list(
        (await db.execute(select(AlertRule).where(AlertRule.enabled.is_(True)))).scalars().all()
    )


async def update_alert_rule(
    db: AsyncSession,
    rule_id: uuid.UUID,
    *,
    name: str | None = None,
    metric: str | None = None,
    scope_customer_id: uuid.UUID | None | object = ...,
    scope_branch_code: str | None | object = ...,
    condition: str | None = None,
    threshold: Decimal | None = None,
    channel: str | None = None,
    channel_target: str | None = None,
    enabled: bool | None = None,
) -> AlertRule:
    """scope_customer_id/scope_branch_code use `...` (Ellipsis) as the "not provided" sentinel
    instead of None, since None is itself a valid value to set (clearing a scope back to
    global) — same reasoning as every other PATCH in this codebase that must distinguish
    "omitted" from "explicitly null".
    """
    rule = await db.get(AlertRule, rule_id)
    if rule is None:
        raise AlertRuleNotFoundError(f"No alert rule with id={rule_id}")
    if metric is not None:
        _validate_metric(metric)
        rule.metric = metric
    if name is not None:
        rule.name = name
    if scope_customer_id is not ...:
        rule.scope_customer_id = scope_customer_id  # type: ignore[assignment]
    if scope_branch_code is not ...:
        rule.scope_branch_code = scope_branch_code  # type: ignore[assignment]
    if condition is not None:
        rule.condition = condition
    if threshold is not None:
        rule.threshold = threshold
    if channel is not None:
        rule.channel = channel
    if channel_target is not None:
        rule.channel_target = channel_target
    if enabled is not None:
        rule.enabled = enabled
    await db.flush()
    return rule


@dataclass
class PreviewResult:
    fired_count_last_7_days: int
    evaluated_as: str  # "historical_7d" | "current_state_only"


async def backtest_kra_call_failure_rate(
    db: AsyncSession,
    condition: str,
    threshold: float,
    scope_branch_code: str | None = None,
    bucket_hours: int = 1,
    days: int = 7,
) -> int:
    """The one metric with genuine historical granularity (KraCallLog/VscuCallLog are
    append-only, timestamped rows) — buckets the last `days` days by `bucket_hours` and
    counts how many buckets would have crossed the threshold. See this module's and
    metrics_service.HISTORICALLY_BACKTESTABLE_METRICS's docstrings for why the other 3
    metrics can't do this.
    """
    since = datetime.now(UTC) - timedelta(days=days)
    branch_id: uuid.UUID | None = None
    if scope_branch_code is not None:
        branch_id = await branch_id_for_code(db, scope_branch_code)
        if branch_id is None:
            return 0

    kra_stmt = select(KraCallLog.created_at, KraCallLog.result_cd).where(
        KraCallLog.created_at >= since
    )
    vscu_stmt = select(VscuCallLog.created_at, VscuCallLog.result_cd).where(
        VscuCallLog.created_at >= since
    )
    if branch_id is not None:
        kra_stmt = kra_stmt.where(KraCallLog.branch_id == branch_id)
        vscu_stmt = vscu_stmt.where(VscuCallLog.branch_id == branch_id)

    rows = [*(await db.execute(kra_stmt)).all(), *(await db.execute(vscu_stmt)).all()]
    bucket_seconds = bucket_hours * 3600
    buckets: dict[int, list[str | None]] = {}
    for created_at, result_cd in rows:
        key = int(created_at.timestamp() // bucket_seconds)
        buckets.setdefault(key, []).append(result_cd)

    fired = 0
    for codes in buckets.values():
        successes = sum(1 for c in codes if c in SUCCESS_RESULT_CODES)
        failure_rate = 1.0 - (successes / len(codes))
        if evaluate_condition(failure_rate, condition, threshold):
            fired += 1
    return fired
