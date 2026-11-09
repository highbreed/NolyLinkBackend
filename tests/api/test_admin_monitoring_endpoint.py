"""API tests for GET /v1/admin/monitoring/* — real dashboard/timeseries/outbox/sync-status
backend, previously entirely mocked on the frontend (no endpoint existed at all).
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.db.models import Branch, Customer, KraCallLog, KraSyncState, Outbox
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


def _tin() -> str:
    return f"A{uuid.uuid4().int % 10**9:09d}Z"


async def test_summary_requires_admin(client):
    response = await client.get("/v1/admin/monitoring/summary")
    assert response.status_code == 403


async def test_summary_reports_outbox_counts_and_success_rate(client, db_session, admin_headers):
    db_session.add_all(
        [
            KraCallLog(endpoint="/e", request_hash="h", result_cd="000"),
            KraCallLog(endpoint="/e", request_hash="h", result_cd="999"),
            Outbox(endpoint="/e", payload={}, status="PENDING"),
            Outbox(endpoint="/e", payload={}, status="FAILED"),
        ]
    )
    await db_session.flush()

    response = await client.get("/v1/admin/monitoring/summary", headers=admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["kra_call_success_rate_24h"] == 0.5
    assert body["outbox_counts"]["PENDING"] == 1
    assert body["outbox_counts"]["FAILED"] == 1
    assert body["stale_branch_count"] == 0
    assert body["blocked_customer_count"] == 0
    assert body["worst_sync_offender"] is None


async def test_kra_calls_timeseries_buckets_by_result_cd(client, db_session, admin_headers):
    db_session.add_all(
        [
            KraCallLog(endpoint="/e", request_hash="h", result_cd="000"),
            KraCallLog(endpoint="/e", request_hash="h", result_cd="000"),
            KraCallLog(endpoint="/e", request_hash="h", result_cd="999"),
        ]
    )
    await db_session.flush()

    response = await client.get(
        "/v1/admin/monitoring/kra-calls/timeseries",
        params={"range_hours": 1, "bucket_minutes": 60},
        headers=admin_headers,
    )
    assert response.status_code == 200
    points = response.json()
    assert len(points) == 1
    assert points[0]["counts_by_result_cd"] == {"000": 2, "999": 1}


async def test_outbox_list_paginates_and_filters_by_status(client, db_session, admin_headers):
    for i in range(3):
        db_session.add(Outbox(endpoint=f"/e{i}", payload={}, status="PENDING"))
    db_session.add(Outbox(endpoint="/e-failed", payload={}, status="FAILED"))
    await db_session.flush()

    response = await client.get(
        "/v1/admin/monitoring/outbox",
        params={"status": "PENDING", "page_size": 2},
        headers=admin_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2
    assert all(item["status"] == "PENDING" for item in body["items"])


async def test_outbox_list_free_text_search_matches_endpoint_or_branch(client, db_session, admin_headers):
    tin = _tin()
    branch = Branch(branch_code="BR-SEARCHME", tin=tin, bhf_id="00", cmc_key="c" * 10)
    db_session.add(branch)
    await db_session.flush()
    db_session.add_all(
        [
            Outbox(endpoint="/trnsSales/saveSales", payload={}, status="PENDING", branch_id=branch.id),
            Outbox(endpoint="/stockMaster/saveStockMaster", payload={}, status="PENDING"),
        ]
    )
    await db_session.flush()

    response = await client.get(
        "/v1/admin/monitoring/outbox", params={"q": "searchme"}, headers=admin_headers
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["endpoint"] for item in items] == ["/trnsSales/saveSales"]


async def test_outbox_retry_resets_attempts_and_status(client, db_session, admin_headers):
    row = Outbox(endpoint="/e", payload={}, status="FAILED", attempts=5, last_error="boom")
    db_session.add(row)
    await db_session.flush()

    response = await client.post(f"/v1/admin/monitoring/outbox/{row.id}/retry", headers=admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "PENDING"
    assert body["attempts"] == 0
    assert body["last_error"] is None


async def test_outbox_cancel_sets_cancelled_status(client, db_session, admin_headers):
    row = Outbox(endpoint="/e", payload={}, status="RETRYING")
    db_session.add(row)
    await db_session.flush()

    response = await client.post(f"/v1/admin/monitoring/outbox/{row.id}/cancel", headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"


async def test_outbox_retry_404_for_unknown_id(client, admin_headers):
    response = await client.post(
        f"/v1/admin/monitoring/outbox/{uuid.uuid4()}/retry", headers=admin_headers
    )
    assert response.status_code == 404


async def test_outbox_item_includes_branch_code_and_customer(client, db_session, admin_headers):
    tin = _tin()
    branch = Branch(branch_code="BR-OUTBOX-1", tin=tin, bhf_id="00", cmc_key="c" * 10)
    db_session.add(branch)
    await db_session.flush()
    customer = Customer(name="Outbox Customer", allowed_branch_ids=[branch.id])
    db_session.add(customer)
    await db_session.flush()
    row = Outbox(endpoint="/e", payload={}, status="PENDING", branch_id=branch.id)
    db_session.add(row)
    await db_session.flush()

    response = await client.get(f"/v1/admin/monitoring/outbox/{row.id}", headers=admin_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["branch_code"] == "BR-OUTBOX-1"
    assert body["customer_id"] == str(customer.id)
    assert body["customer_name"] == "Outbox Customer"


async def test_sync_status_reports_lag_seconds(client, db_session, admin_headers):
    tin = _tin()
    branch = Branch(branch_code="BR-SYNC-1", tin=tin, bhf_id="00", cmc_key="c" * 10)
    db_session.add(branch)
    await db_session.flush()
    row = KraSyncState(tin=tin, bhf_id="00", resource="codes", last_req_dt="20260101000000")
    db_session.add(row)
    await db_session.flush()
    row.updated_at = datetime.now(UTC) - timedelta(minutes=5)
    await db_session.flush()

    response = await client.get("/v1/admin/monitoring/sync-status", headers=admin_headers)
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["branch_code"] == "BR-SYNC-1"
    assert rows[0]["lag_seconds"] >= 290
