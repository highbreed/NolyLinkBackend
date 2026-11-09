"""Tests for app/services/admin_audit.py's actor-string parsing and row creation."""

from sqlalchemy import select

from app.db.models import AdminAuditLog
from app.services import admin_audit


async def test_record_admin_action_parses_user_actor(db_session):
    await admin_audit.record_admin_action(
        db_session,
        actor="user:alice",
        action="customer.create",
        target_type="customer",
        target_id="c1",
        after={"name": "Acme"},
    )
    row = (await db_session.execute(select(AdminAuditLog))).scalar_one()
    assert row.admin_user_email == "alice"
    assert row.action == "customer.create"
    assert row.after_state == {"name": "Acme"}
    assert row.before_state is None


async def test_record_admin_action_api_key_actor_is_none(db_session):
    await admin_audit.record_admin_action(
        db_session,
        actor="api-key",
        action="device.initialize",
        target_type="branch",
        target_id="b1",
    )
    row = (await db_session.execute(select(AdminAuditLog))).scalar_one()
    assert row.admin_user_email is None


async def test_record_admin_action_captures_reason(db_session):
    await admin_audit.record_admin_action(
        db_session,
        actor="user:bob",
        action="subscription.suspend",
        target_type="customer",
        target_id="c2",
        reason="chargeback",
    )
    row = (await db_session.execute(select(AdminAuditLog))).scalar_one()
    assert row.reason == "chargeback"
