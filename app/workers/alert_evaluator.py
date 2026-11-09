"""Periodic alert-rule evaluation + delivery (spec: this gateway's own tenant-ops tooling,
entirely outside the KRA spec). Every 5 minutes (app/workers/celery_app.py's beat_schedule):
loads every enabled AlertRule, computes its metric's current value via
metrics_service.get_current_metric_value (the same function the preview endpoint falls back
to for the 3 non-backtestable metrics, so "does this fire" is never computed two different
ways), and on a threshold breach, debounces (skipped if a NotificationSent for that rule
exists within DEBOUNCE_MINUTES) then records a row and attempts delivery:
  - webhook: a real httpx POST to channel_target.
  - email: always delivered=False, delivery_error="SMTP not configured" — no SMTP credentials
    were available when this was built (see deploy docs); flip this on once real settings
    exist in .env, same pattern as every other secret in this project.
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import AlertRule, NotificationSent
from app.db.session import AsyncSessionLocal
from app.services import alert_rules_service, metrics_service
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

DEBOUNCE_MINUTES = 60


def _summary(rule: AlertRule, value: float) -> str:
    scope = ""
    if rule.scope_branch_code:
        scope = f" for branch {rule.scope_branch_code}"
    elif rule.scope_customer_id:
        scope = f" for customer {rule.scope_customer_id}"
    return f"{rule.metric}={value:g} {rule.condition} {rule.threshold}{scope} ({rule.name})"


async def _recently_notified(db: AsyncSession, rule_id: uuid.UUID, now: datetime) -> bool:
    cutoff = now - timedelta(minutes=DEBOUNCE_MINUTES)
    existing = await db.scalar(
        select(NotificationSent.id).where(
            NotificationSent.alert_rule_id == rule_id, NotificationSent.fired_at >= cutoff
        )
    )
    return existing is not None


async def _deliver(rule: AlertRule, summary: str) -> tuple[bool, str | None]:
    if rule.channel == "webhook":
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    rule.channel_target,
                    json={"rule_name": rule.name, "summary": summary},
                )
                response.raise_for_status()
            return True, None
        except httpx.HTTPError as exc:
            return False, str(exc)
    # channel == "email": no SMTP configured, by design (see module docstring).
    return False, "SMTP not configured"


async def _evaluate_alert_rules(
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
) -> int:
    now = datetime.now(UTC)
    fired_count = 0
    async with session_factory() as db:
        rules = await alert_rules_service.list_enabled_alert_rules(db)
        for rule in rules:
            value = await metrics_service.get_current_metric_value(
                db,
                rule.metric,
                scope_customer_id=rule.scope_customer_id,
                scope_branch_code=rule.scope_branch_code,
            )
            if value is None:
                continue
            if not alert_rules_service.evaluate_condition(
                value, rule.condition, float(rule.threshold)
            ):
                continue
            if await _recently_notified(db, rule.id, now):
                continue

            summary = _summary(rule, value)
            delivered, delivery_error = await _deliver(rule, summary)
            db.add(
                NotificationSent(
                    alert_rule_id=rule.id,
                    metric_value=value,
                    summary=summary,
                    delivered=delivered,
                    delivery_error=delivery_error,
                )
            )
            await db.commit()
            fired_count += 1
            if not delivered:
                logger.warning(
                    "Alert rule %s (%s) fired but delivery failed: %s",
                    rule.id,
                    rule.name,
                    delivery_error,
                )
    return fired_count


@celery_app.task(name="alert_evaluator.evaluate_alert_rules")  # type: ignore[untyped-decorator]
def evaluate_alert_rules() -> int:
    return asyncio.run(_evaluate_alert_rules())
