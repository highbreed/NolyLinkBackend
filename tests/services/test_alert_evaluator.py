"""Tests for app/workers/alert_evaluator.py — debounce, webhook delivery, and the
no-SMTP-configured email path.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import respx
from httpx import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.db.models import AlertRule, Branch, KraCallLog, NotificationSent
from app.workers.alert_evaluator import _evaluate_alert_rules


@pytest.fixture
def session_factory(db_engine: AsyncEngine):
    return async_sessionmaker(bind=db_engine, expire_on_commit=False)


def _tin() -> str:
    return f"A{uuid.uuid4().int % 10**9:09d}Z"


@pytest.fixture(autouse=True)
async def _cleanup(session_factory):
    from sqlalchemy import delete

    yield
    async with session_factory() as db:
        await db.execute(delete(NotificationSent))
        await db.execute(delete(AlertRule))
        branch_ids = (
            (
                await db.execute(
                    select(Branch.id).where(Branch.branch_code.like("BR-EVAL-%"))
                )
            )
            .scalars()
            .all()
        )
        if branch_ids:
            await db.execute(delete(KraCallLog).where(KraCallLog.branch_id.in_(branch_ids)))
            await db.execute(delete(Branch).where(Branch.id.in_(branch_ids)))
        await db.commit()


async def test_webhook_delivery_succeeds_and_records_notification(session_factory):
    async with session_factory() as db:
        branch = Branch(branch_code="BR-EVAL-1", tin=_tin(), bhf_id="00", cmc_key="c" * 10)
        db.add(branch)
        await db.flush()
        db.add(
            KraCallLog(branch_id=branch.id, endpoint="/e", request_hash="h", result_cd="999")
        )
        rule = AlertRule(
            name="Webhook rule",
            metric="kra_call_failure_rate",
            scope_branch_code="BR-EVAL-1",
            condition=">",
            threshold=Decimal("0.0"),
            channel="webhook",
            channel_target="https://hooks.example.com/alert",
            enabled=True,
        )
        db.add(rule)
        await db.commit()
        rule_id = rule.id

    with respx.mock(base_url="https://hooks.example.com") as mock:
        mock.post("/alert").mock(return_value=Response(200, json={"ok": True}))
        fired = await _evaluate_alert_rules(session_factory=session_factory)

    assert fired == 1
    async with session_factory() as db:
        notification = (await db.execute(select(NotificationSent))).scalar_one()
    assert notification.alert_rule_id == rule_id
    assert notification.delivered is True
    assert notification.delivery_error is None


async def test_email_channel_always_records_smtp_not_configured(session_factory):
    async with session_factory() as db:
        rule = AlertRule(
            name="Email rule",
            metric="outbox_failed_count",
            condition=">=",
            threshold=Decimal("0"),
            channel="email",
            channel_target="ops@example.com",
            enabled=True,
        )
        db.add(rule)
        await db.commit()

    fired = await _evaluate_alert_rules(session_factory=session_factory)
    assert fired == 1
    async with session_factory() as db:
        notification = (await db.execute(select(NotificationSent))).scalar_one()
    assert notification.delivered is False
    assert notification.delivery_error == "SMTP not configured"


async def test_disabled_rule_is_never_evaluated(session_factory):
    async with session_factory() as db:
        db.add(
            AlertRule(
                name="Disabled rule",
                metric="outbox_failed_count",
                condition=">=",
                threshold=Decimal("0"),
                channel="webhook",
                channel_target="https://example.com",
                enabled=False,
            )
        )
        await db.commit()

    fired = await _evaluate_alert_rules(session_factory=session_factory)
    assert fired == 0


async def test_debounce_skips_a_rule_notified_within_the_last_hour(session_factory):
    async with session_factory() as db:
        rule = AlertRule(
            name="Debounced rule",
            metric="outbox_failed_count",
            condition=">=",
            threshold=Decimal("0"),
            channel="email",
            channel_target="ops@example.com",
            enabled=True,
        )
        db.add(rule)
        await db.flush()
        db.add(
            NotificationSent(
                alert_rule_id=rule.id,
                metric_value=Decimal("0"),
                summary="already fired",
                delivered=False,
                delivery_error="SMTP not configured",
                fired_at=datetime.now(UTC) - timedelta(minutes=5),
            )
        )
        await db.commit()

    fired = await _evaluate_alert_rules(session_factory=session_factory)
    assert fired == 0  # debounced — not a second notification within the hour

    async with session_factory() as db:
        count = len((await db.execute(select(NotificationSent))).scalars().all())
    assert count == 1
