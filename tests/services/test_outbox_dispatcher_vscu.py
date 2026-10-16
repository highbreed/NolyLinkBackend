"""Tests confirming outbox_dispatcher.py correctly routes a VSCU-marked row through
VSCUClient (not KraAdapter) purely by its endpoint string, and classifies VSCU's own result
codes correctly: 924 as success-equivalent, 836 as a permanent (non-retried) failure, 991-type
codes retried with backoff — mirroring tests/services/test_outbox_dispatcher.py's OSCU tests.
"""

import uuid

import pytest
import respx
from httpx import Response
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.config import get_settings
from app.db.models import Branch, Outbox, VscuCallLog, VscuDeviceState
from app.services.vscu_stock_service import VSCU_STOCK_MASTER_ENDPOINT
from app.workers.outbox_dispatcher import _dispatch_pending

VSCU_URL = "http://vscu-branch.test:8088"


@pytest.fixture(autouse=True)
def enable_vscu(monkeypatch):
    # _dispatch_pending builds its own VSCUClient from get_settings() (real env config, not
    # test-injected settings) — vscu_enabled defaults False, so without this every dispatch
    # here would fail at VSCUClient._resolve_device before ever making the HTTP call.
    monkeypatch.setenv("VSCU_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def session_factory(db_engine: AsyncEngine):
    return async_sessionmaker(bind=db_engine, expire_on_commit=False)


@pytest.fixture
async def branch(session_factory):
    async with session_factory() as db:
        b = Branch(
            branch_code=f"VSCU-DISP-{uuid.uuid4().hex[:8]}",
            tin=f"A{uuid.uuid4().int % 10**9:09d}Z",
            bhf_id="00",
            cmc_key="unused",
            device_type="VSCU",
        )
        db.add(b)
        await db.flush()
        db.add(VscuDeviceState(branch_id=b.id, base_url=VSCU_URL, dvc_srl_no="SN-1"))
        await db.commit()
        await db.refresh(b)

    yield b

    async with session_factory() as db:
        await db.execute(delete(Outbox).where(Outbox.branch_id == b.id))
        await db.execute(delete(VscuCallLog).where(VscuCallLog.branch_id == b.id))
        await db.execute(delete(VscuDeviceState).where(VscuDeviceState.branch_id == b.id))
        await db.execute(delete(Branch).where(Branch.id == b.id))
        await db.commit()


def stock_master_payload(branch: Branch) -> dict:
    return {
        "tin": branch.tin,
        "bhfId": branch.bhf_id,
        "itemCd": "KE2NTBA0000001",
        "rsdQty": "10.00",
        "regrId": "gateway",
        "regrNm": "VSCU Gateway",
        "modrId": "gateway",
        "modrNm": "VSCU Gateway",
    }


def envelope(result_cd: str = "000", result_msg: str = "ok") -> dict:
    return {
        "resultCd": result_cd,
        "resultMsg": result_msg,
        "resultDt": "20260901120000",
        "data": None,
    }


async def test_vscu_row_dispatched_through_vscu_client_and_marked_sent(session_factory, branch):
    async with session_factory() as db:
        row = Outbox(
            endpoint=VSCU_STOCK_MASTER_ENDPOINT,
            branch_id=branch.id,
            payload=stock_master_payload(branch),
        )
        db.add(row)
        await db.commit()
        row_id = row.id

    with respx.mock:
        route = respx.post(f"{VSCU_URL}/stockMaster/saveStockMaster").mock(
            return_value=Response(200, json=envelope())
        )
        await _dispatch_pending(session_factory)

    assert route.call_count == 1
    async with session_factory() as db:
        refreshed = await db.get(Outbox, row_id)
        assert refreshed.status == "SENT"
        assert refreshed.attempts == 0


async def test_vscu_duplicate_is_treated_as_success(session_factory, branch):
    async with session_factory() as db:
        row = Outbox(
            endpoint=VSCU_STOCK_MASTER_ENDPOINT,
            branch_id=branch.id,
            payload=stock_master_payload(branch),
        )
        db.add(row)
        await db.commit()
        row_id = row.id

    with respx.mock:
        respx.post(f"{VSCU_URL}/stockMaster/saveStockMaster").mock(
            return_value=Response(200, json=envelope("924", "INVOICE NUMBER IS ALREADY PROCESSED"))
        )
        await _dispatch_pending(session_factory)

    async with session_factory() as db:
        refreshed = await db.get(Outbox, row_id)
        assert refreshed.status == "SENT"


async def test_vscu_sequence_error_fails_permanently_without_retry(session_factory, branch):
    async with session_factory() as db:
        row = Outbox(
            endpoint=VSCU_STOCK_MASTER_ENDPOINT,
            branch_id=branch.id,
            payload=stock_master_payload(branch),
        )
        db.add(row)
        await db.commit()
        row_id = row.id

    with respx.mock:
        respx.post(f"{VSCU_URL}/stockMaster/saveStockMaster").mock(
            return_value=Response(200, json=envelope("836", "Your Sequences have been altered"))
        )
        await _dispatch_pending(session_factory)

    async with session_factory() as db:
        refreshed = await db.get(Outbox, row_id)
        assert refreshed.status == "FAILED"
        assert refreshed.attempts == 1
        assert "836" in refreshed.last_error


async def test_vscu_transient_error_retries_with_backoff(session_factory, branch):
    async with session_factory() as db:
        row = Outbox(
            endpoint=VSCU_STOCK_MASTER_ENDPOINT,
            branch_id=branch.id,
            payload=stock_master_payload(branch),
        )
        db.add(row)
        await db.commit()
        row_id = row.id

    with respx.mock:
        respx.post(f"{VSCU_URL}/stockMaster/saveStockMaster").mock(
            return_value=Response(200, json=envelope("991", "server error"))
        )
        await _dispatch_pending(session_factory)

    async with session_factory() as db:
        refreshed = await db.get(Outbox, row_id)
        assert refreshed.status == "RETRYING"
        assert refreshed.attempts == 1
