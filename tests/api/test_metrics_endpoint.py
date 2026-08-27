"""Tests for GET /metrics (Phase 10): request latency histogram, KRA call counter by
resultCd, outbox queue depth, sync lag per resource.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from app.core.config import get_settings
from app.db.models import Branch, KraSyncState, Outbox
from app.db.session import get_db
from app.main import app
from app.schemas.kra.codes import CodeSearchReq
from app.services.kra_adapter import KraAdapter

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "kra_samples"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text())


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


async def test_metrics_endpoint_exposes_expected_metric_names(client):
    async with client as c:
        response = await c.get("/metrics")

    assert response.status_code == 200
    text = response.text
    assert "http_request_duration_seconds" in text
    assert "kra_calls_total" in text
    assert "outbox_queue_depth" in text
    assert "kra_sync_lag_seconds" in text


async def test_http_request_latency_is_recorded(client):
    async with client as c:
        await c.get("/v1/health")
        response = await c.get("/metrics")

    # Path must be the route template ("/v1/health"), not a resolved URL with embedded IDs.
    assert 'path="/v1/health"' in response.text


async def test_path_params_are_templated_not_leaked_into_labels(client):
    """The whole point of _route_template: a path with an embedded ID (e.g. an invoice ref)
    must collapse to one label value, not a fresh one per distinct ID — otherwise this metric
    grows unbounded in production.
    """
    async with client as c:
        await c.get("/v1/sales-invoices/SAP-BILLDOC-001", params={"branch_code": "X"})
        await c.get("/v1/sales-invoices/SAP-BILLDOC-002", params={"branch_code": "X"})
        response = await c.get("/metrics")

    assert 'path="/v1/sales-invoices/{external_ref}"' in response.text
    assert "SAP-BILLDOC-001" not in response.text
    assert "SAP-BILLDOC-002" not in response.text


async def test_kra_call_counter_increments_by_result_cd(client, db_session):
    branch = Branch(branch_code="NAIROBI-HQ", tin="A123456789Z", bhf_id="00", cmc_key="c" * 10)
    db_session.add(branch)
    await db_session.flush()

    settings = get_settings()
    with respx.mock(base_url=settings.kra_base_url) as mock:
        mock.post("/selectCodeList").mock(
            return_value=Response(200, json=load_fixture("code_search_res"))
        )
        kra = KraAdapter(db=db_session)
        req = CodeSearchReq.model_validate(load_fixture("code_search_req"))
        await kra.get_code_list(req, branch_id=branch.id)
        await kra.aclose()
        await db_session.commit()

    async with client as c:
        response = await c.get("/metrics")

    assert 'endpoint="/selectCodeList",result_cd="000"' in response.text


async def test_outbox_queue_depth_reflects_db_state(client, db_session):
    branch = Branch(branch_code="NAIROBI-HQ", tin="A123456789Z", bhf_id="00", cmc_key="c" * 10)
    db_session.add(branch)
    await db_session.flush()
    db_session.add(Outbox(endpoint="/saveStockMaster", branch_id=branch.id, payload={}))
    db_session.add(Outbox(endpoint="/saveStockMaster", branch_id=branch.id, payload={}))
    await db_session.flush()

    async with client as c:
        response = await c.get("/metrics")

    lines = [
        line
        for line in response.text.splitlines()
        if line.startswith('outbox_queue_depth{status="PENDING"}')
    ]
    assert len(lines) == 1
    assert lines[0].endswith(" 2.0")


async def test_sync_lag_reflects_time_since_last_poll(client, db_session):
    stale_time = datetime.now(UTC) - timedelta(hours=2)
    db_session.add(
        KraSyncState(
            tin="A123456789Z",
            bhf_id="00",
            resource="codes",
            last_req_dt="20200101000000",
            updated_at=stale_time,
        )
    )
    await db_session.flush()

    async with client as c:
        response = await c.get("/metrics")

    lines = [
        line
        for line in response.text.splitlines()
        if line.startswith('kra_sync_lag_seconds{resource="codes"}')
    ]
    assert len(lines) == 1
    lag = float(lines[0].split(" ")[-1])
    assert lag > 3600  # more than an hour stale, matching the ~2h fixture above
