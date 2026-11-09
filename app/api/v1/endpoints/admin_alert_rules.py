"""Real backend for the admin portal's alert rules + notifications pages — previously an
in-browser mock with no server-side rule storage, evaluation, or delivery at all.

The preview endpoint is honest about what it can and can't backtest: kra_call_failure_rate
has real historical data to look back over (KraCallLog/VscuCallLog are append-only,
timestamped); the other three metrics only know their CURRENT value (Outbox/KraSyncState/
Subscription rows don't retain history of their own past states) — see
metrics_service.HISTORICALLY_BACKTESTABLE_METRICS and alert_rules_service's own docstring.
Real evaluation + delivery lives in app/workers/alert_evaluator.py.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_auth import require_admin
from app.db.models import AlertRule, NotificationSent
from app.db.session import get_db
from app.schemas.page import Page
from app.services import alert_rules_service, metrics_service

router = APIRouter(dependencies=[Depends(require_admin)])


class AlertRuleInputBase(BaseModel):
    name: str
    metric: str
    scope_customer_id: uuid.UUID | None = None
    scope_branch_code: str | None = None
    condition: str
    threshold: Decimal
    channel: str
    channel_target: str
    enabled: bool = True


class AlertRuleOut(BaseModel):
    id: uuid.UUID
    name: str
    metric: str
    scope_customer_id: uuid.UUID | None
    scope_branch_code: str | None
    condition: str
    threshold: Decimal
    channel: str
    channel_target: str
    enabled: bool
    created_at: datetime


def _to_out(rule: AlertRule) -> AlertRuleOut:
    return AlertRuleOut(
        id=rule.id,
        name=rule.name,
        metric=rule.metric,
        scope_customer_id=rule.scope_customer_id,
        scope_branch_code=rule.scope_branch_code,
        condition=rule.condition,
        threshold=rule.threshold,
        channel=rule.channel,
        channel_target=rule.channel_target,
        enabled=rule.enabled,
        created_at=rule.created_at,
    )


@router.get("", response_model=list[AlertRuleOut])
async def list_alert_rules(db: AsyncSession = Depends(get_db)) -> list[AlertRuleOut]:
    rules = await alert_rules_service.list_alert_rules(db)
    return [_to_out(r) for r in rules]


@router.post("", response_model=AlertRuleOut, status_code=201)
async def create_alert_rule(
    payload: AlertRuleInputBase, db: AsyncSession = Depends(get_db)
) -> AlertRuleOut:
    try:
        rule = await alert_rules_service.create_alert_rule(
            db,
            name=payload.name,
            metric=payload.metric,
            scope_customer_id=payload.scope_customer_id,
            scope_branch_code=payload.scope_branch_code,
            condition=payload.condition,
            threshold=payload.threshold,
            channel=payload.channel,
            channel_target=payload.channel_target,
            enabled=payload.enabled,
        )
        await db.commit()
    except alert_rules_service.UnknownMetricError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _to_out(rule)


class AlertRuleUpdate(BaseModel):
    name: str | None = None
    metric: str | None = None
    scope_customer_id: uuid.UUID | None = None
    scope_branch_code: str | None = None
    condition: str | None = None
    threshold: Decimal | None = None
    channel: str | None = None
    channel_target: str | None = None
    enabled: bool | None = None


@router.patch("/{rule_id}", response_model=AlertRuleOut)
async def update_alert_rule(
    rule_id: uuid.UUID, payload: AlertRuleUpdate, db: AsyncSession = Depends(get_db)
) -> AlertRuleOut:
    fields = payload.model_dump(exclude_unset=True)
    try:
        rule = await alert_rules_service.update_alert_rule(db, rule_id, **fields)
        await db.commit()
    except alert_rules_service.AlertRuleNotFoundError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except alert_rules_service.UnknownMetricError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _to_out(rule)


class AlertRulePreviewOut(BaseModel):
    fired_count_last_7_days: int
    evaluated_as: str


@router.get("/preview", response_model=AlertRulePreviewOut)
async def preview_alert_rule(
    metric: str,
    condition: str,
    threshold: Decimal,
    scope_customer_id: uuid.UUID | None = None,
    scope_branch_code: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> AlertRulePreviewOut:
    if metric not in metrics_service.KNOWN_METRICS:
        raise HTTPException(status_code=422, detail=f"Unknown metric: {metric!r}")

    if metric in metrics_service.HISTORICALLY_BACKTESTABLE_METRICS:
        fired = await alert_rules_service.backtest_kra_call_failure_rate(
            db, condition, float(threshold), scope_branch_code=scope_branch_code
        )
        return AlertRulePreviewOut(fired_count_last_7_days=fired, evaluated_as="historical_7d")

    value = await metrics_service.get_current_metric_value(
        db, metric, scope_customer_id=scope_customer_id, scope_branch_code=scope_branch_code
    )
    fires_now = value is not None and alert_rules_service.evaluate_condition(
        value, condition, float(threshold)
    )
    return AlertRulePreviewOut(
        fired_count_last_7_days=1 if fires_now else 0, evaluated_as="current_state_only"
    )


class NotificationSentOut(BaseModel):
    id: int
    alert_rule_id: uuid.UUID
    alert_rule_name: str
    fired_at: datetime
    resolved_at: datetime | None
    summary: str


notifications_router = APIRouter(dependencies=[Depends(require_admin)])


@notifications_router.get("", response_model=Page[NotificationSentOut])
async def list_notifications(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, le=200, ge=1),
    db: AsyncSession = Depends(get_db),
) -> Page[NotificationSentOut]:
    rows = (
        (
            await db.execute(
                select(NotificationSent, AlertRule.name)
                .join(AlertRule, NotificationSent.alert_rule_id == AlertRule.id)
                .order_by(NotificationSent.fired_at.desc())
            )
        )
        .all()
    )
    total = len(rows)
    start = (page - 1) * page_size
    page_rows = rows[start : start + page_size]
    return Page[NotificationSentOut](
        items=[
            NotificationSentOut(
                id=n.id,
                alert_rule_id=n.alert_rule_id,
                alert_rule_name=rule_name,
                fired_at=n.fired_at,
                resolved_at=n.resolved_at,
                summary=n.summary,
            )
            for n, rule_name in page_rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )
