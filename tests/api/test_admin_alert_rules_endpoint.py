"""API tests for /v1/admin/alert-rules and /v1/admin/notifications — real backend, previously
mocked with no server-side storage, evaluation, or preview at all.
"""

from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.db.models import AlertRule, Branch, KraCallLog, NotificationSent
from app.db.session import get_db
from app.main import app


@pytest.fixture
def client(db_session):
    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    try:
        transport = ASGITransport(app=app)
        yield AsyncClient(transport=transport, base_url="http://test")
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def admin_headers() -> dict[str, str]:
    admin_key = get_settings().admin_api_key
    assert admin_key, "ADMIN_API_KEY must be configured for this test run"
    return {"X-Admin-Api-Key": admin_key}


async def test_create_list_and_update_alert_rule(client, admin_headers):
    create_response = await client.post(
        "/v1/admin/alert-rules",
        headers=admin_headers,
        json={
            "name": "High failure rate",
            "metric": "kra_call_failure_rate",
            "condition": ">",
            "threshold": "0.1",
            "channel": "webhook",
            "channel_target": "https://example.com/hook",
            "enabled": True,
        },
    )
    assert create_response.status_code == 201
    rule_id = create_response.json()["id"]

    list_response = await client.get("/v1/admin/alert-rules", headers=admin_headers)
    assert list_response.status_code == 200
    assert len(list_response.json()) == 1

    update_response = await client.patch(
        f"/v1/admin/alert-rules/{rule_id}", headers=admin_headers, json={"enabled": False}
    )
    assert update_response.status_code == 200
    assert update_response.json()["enabled"] is False


async def test_create_alert_rule_rejects_unknown_metric(client, admin_headers):
    response = await client.post(
        "/v1/admin/alert-rules",
        headers=admin_headers,
        json={
            "name": "Bad",
            "metric": "not_a_real_metric",
            "condition": ">",
            "threshold": "1",
            "channel": "webhook",
            "channel_target": "https://example.com",
            "enabled": True,
        },
    )
    assert response.status_code == 422


async def test_preview_historical_for_kra_call_failure_rate(client, db_session, admin_headers):
    branch = Branch(branch_code="BR-PREVIEW", tin="A111111199Z", bhf_id="00", cmc_key="c" * 10)
    db_session.add(branch)
    await db_session.flush()
    db_session.add_all(
        [
            KraCallLog(branch_id=branch.id, endpoint="/e", request_hash="h", result_cd="999"),
            KraCallLog(branch_id=branch.id, endpoint="/e", request_hash="h", result_cd="999"),
        ]
    )
    await db_session.flush()

    response = await client.get(
        "/v1/admin/alert-rules/preview",
        params={
            "metric": "kra_call_failure_rate",
            "condition": ">",
            "threshold": "0.5",
            "scope_branch_code": "BR-PREVIEW",
        },
        headers=admin_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["evaluated_as"] == "historical_7d"
    assert body["fired_count_last_7_days"] == 1


async def test_preview_current_state_only_for_outbox_failed_count(client, admin_headers):
    response = await client.get(
        "/v1/admin/alert-rules/preview",
        params={"metric": "outbox_failed_count", "condition": ">", "threshold": "100"},
        headers=admin_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["evaluated_as"] == "current_state_only"
    assert body["fired_count_last_7_days"] == 0


async def test_preview_rejects_unknown_metric(client, admin_headers):
    response = await client.get(
        "/v1/admin/alert-rules/preview",
        params={"metric": "nope", "condition": ">", "threshold": "1"},
        headers=admin_headers,
    )
    assert response.status_code == 422


async def test_list_notifications_joins_rule_name_and_paginates(client, db_session, admin_headers):
    rule = AlertRule(
        name="Test rule",
        metric="outbox_failed_count",
        condition=">",
        threshold=Decimal("1"),
        channel="webhook",
        channel_target="https://example.com",
    )
    db_session.add(rule)
    await db_session.flush()
    db_session.add_all(
        [
            NotificationSent(
                alert_rule_id=rule.id, metric_value=Decimal("2"), summary="first", delivered=True
            ),
            NotificationSent(
                alert_rule_id=rule.id,
                metric_value=Decimal("3"),
                summary="second",
                delivered=False,
                delivery_error="SMTP not configured",
            ),
        ]
    )
    await db_session.flush()

    response = await client.get("/v1/admin/notifications", headers=admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert all(item["alert_rule_name"] == "Test rule" for item in body["items"])
