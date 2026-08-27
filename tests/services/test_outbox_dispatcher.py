"""Tests for outbox_dispatcher (spec §14): backoff eligibility, permanent-vs-retriable
error classification, duplicate-as-success, and business-record status propagation.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import respx
from httpx import Response
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.config import get_settings
from app.db.models import Branch, KraCallLog, Outbox, Purchase, StockIOSubmission
from app.workers.outbox_dispatcher import BACKOFF_SCHEDULE_SECONDS, _dispatch_pending

BASE_URL = get_settings().kra_base_url


@pytest.fixture
def session_factory(db_engine: AsyncEngine):
    return async_sessionmaker(bind=db_engine, expire_on_commit=False)


@pytest.fixture
async def branch(session_factory):
    # db_engine is session-scoped and this fixture commits directly (bypassing the usual
    # rollback-based db_session isolation, since the dispatcher needs its own real session)
    # — a unique tin per test avoids colliding with other tests in this file, and explicit
    # teardown keeps a committed ACTIVE branch from leaking into other test files that scan
    # for "every ACTIVE branch" against this same session-scoped engine (e.g. test_poller.py).
    async with session_factory() as db:
        b = Branch(
            branch_code=f"BRANCH-{uuid.uuid4().hex[:8]}",
            tin=f"A{uuid.uuid4().int % 10**9:09d}Z",
            bhf_id="00",
            cmc_key="c" * 10,
        )
        db.add(b)
        await db.commit()
        await db.refresh(b)

    yield b

    async with session_factory() as db:
        await db.execute(delete(Purchase).where(Purchase.branch_id == b.id))
        await db.execute(delete(StockIOSubmission).where(StockIOSubmission.branch_id == b.id))
        await db.execute(delete(Outbox).where(Outbox.branch_id == b.id))
        await db.execute(delete(KraCallLog).where(KraCallLog.branch_id == b.id))
        await db.execute(delete(Branch).where(Branch.id == b.id))
        await db.commit()


def stock_master_payload(branch: Branch) -> dict:
    return {
        "tin": branch.tin,
        "bhfId": branch.bhf_id,
        "cmcKey": branch.cmc_key,
        "itemCd": "KE2NTBA0000001",
        "rsdQty": "10.00",
    }


def envelope(result_cd: str = "000", result_msg: str = "It is succeeded") -> dict:
    return {
        "resultCd": result_cd,
        "resultMsg": result_msg,
        "resultDt": "20200226143124",
        "data": None,
    }


async def test_pending_row_dispatched_and_marked_sent(session_factory, branch):
    async with session_factory() as db:
        row = Outbox(
            endpoint="/saveStockMaster", branch_id=branch.id, payload=stock_master_payload(branch)
        )
        db.add(row)
        await db.commit()
        row_id = row.id

    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/saveStockMaster").mock(return_value=Response(200, json=envelope()))
        await _dispatch_pending(session_factory)

    async with session_factory() as db:
        refreshed = await db.get(Outbox, row_id)
        assert refreshed.status == "SENT"
        assert refreshed.attempts == 0
        assert refreshed.last_error is None


async def test_validation_error_fails_permanently_without_retry(session_factory, branch):
    async with session_factory() as db:
        row = Outbox(
            endpoint="/saveStockMaster", branch_id=branch.id, payload=stock_master_payload(branch)
        )
        db.add(row)
        await db.commit()
        row_id = row.id

    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/saveStockMaster").mock(
            return_value=Response(200, json=envelope("891", "bad payload"))
        )
        await _dispatch_pending(session_factory)

    async with session_factory() as db:
        refreshed = await db.get(Outbox, row_id)
        assert refreshed.status == "FAILED"
        assert refreshed.attempts == 1
        assert "891" in refreshed.last_error


async def test_transient_error_retries_with_backoff(session_factory, branch):
    async with session_factory() as db:
        row = Outbox(
            endpoint="/saveStockMaster", branch_id=branch.id, payload=stock_master_payload(branch)
        )
        db.add(row)
        await db.commit()
        row_id = row.id

    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/saveStockMaster").mock(
            return_value=Response(200, json=envelope("991", "server error"))
        )
        await _dispatch_pending(session_factory)

    async with session_factory() as db:
        refreshed = await db.get(Outbox, row_id)
        assert refreshed.status == "RETRYING"
        assert refreshed.attempts == 1

    # Not eligible again immediately — still inside the first backoff window.
    with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        route = mock.post("/saveStockMaster").mock(
            return_value=Response(200, json=envelope("991", "server error"))
        )
        await _dispatch_pending(session_factory)
    assert route.call_count == 0

    # Force the row past its backoff window and confirm it becomes eligible again.
    async with session_factory() as db:
        refreshed = await db.get(Outbox, row_id)
        refreshed.updated_at = datetime.now(UTC) - timedelta(
            seconds=BACKOFF_SCHEDULE_SECONDS[0] + 1
        )
        await db.commit()

    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.post("/saveStockMaster").mock(return_value=Response(200, json=envelope()))
        await _dispatch_pending(session_factory)
    assert route.call_count == 1

    async with session_factory() as db:
        refreshed = await db.get(Outbox, row_id)
        assert refreshed.status == "SENT"


async def test_exhausting_backoff_schedule_marks_permanently_failed(session_factory, branch):
    async with session_factory() as db:
        row = Outbox(
            endpoint="/saveStockMaster",
            branch_id=branch.id,
            payload=stock_master_payload(branch),
            attempts=len(BACKOFF_SCHEDULE_SECONDS),
            status="RETRYING",
        )
        db.add(row)
        await db.commit()
        row.updated_at = datetime.now(UTC) - timedelta(hours=2)
        await db.commit()
        row_id = row.id

    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/saveStockMaster").mock(
            return_value=Response(200, json=envelope("999", "unknown"))
        )
        await _dispatch_pending(session_factory)

    async with session_factory() as db:
        refreshed = await db.get(Outbox, row_id)
        assert refreshed.status == "FAILED"
        assert refreshed.attempts == len(BACKOFF_SCHEDULE_SECONDS) + 1


async def test_duplicate_error_treated_as_success(session_factory, branch):
    async with session_factory() as db:
        row = Outbox(
            endpoint="/saveStockMaster", branch_id=branch.id, payload=stock_master_payload(branch)
        )
        db.add(row)
        await db.commit()
        row_id = row.id

    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/saveStockMaster").mock(
            return_value=Response(200, json=envelope("994", "overlapped data"))
        )
        await _dispatch_pending(session_factory)

    async with session_factory() as db:
        refreshed = await db.get(Outbox, row_id)
        assert refreshed.status == "SENT"


async def test_linked_purchase_status_follows_outbox_status(session_factory, branch):
    async with session_factory() as db:
        row = Outbox(
            endpoint="/insertTrnsPurchase",
            branch_id=branch.id,
            payload={
                "tin": branch.tin,
                "bhfId": branch.bhf_id,
                "cmcKey": branch.cmc_key,
                "invcNo": "1",
                "orgInvcNo": "0",
                "spplrTin": "P987654321B",
                "spplrBhfId": "00",
                "spplrInvcNo": "SUP-1",
                "regTyCd": "M",
                "pchsTyCd": "N",
                "rcptTyCd": "P",
                "pchsSttsCd": "02",
                "pchsDt": "20200226",
                "cfmDt": "20200226143124",
                "salesDt": "20200226",
                "totItemCnt": 0,
                "taxblAmtA": "0.00",
                "taxblAmtB": "0.00",
                "taxblAmtC": "0.00",
                "taxblAmtD": "0.00",
                "taxblAmtE": "0.00",
                "taxRtA": "0.00",
                "taxRtB": "16.00",
                "taxRtC": "0.00",
                "taxRtD": "0.00",
                "taxRtE": "8.00",
                "taxAmtA": "0.00",
                "taxAmtB": "0.00",
                "taxAmtC": "0.00",
                "taxAmtD": "0.00",
                "taxAmtE": "0.00",
                "totTaxblAmt": "0.00",
                "totTaxAmt": "0.00",
                "totAmt": "0.00",
                "regrId": "gateway",
                "regrNm": "OSCU Gateway",
                "modrId": "gateway",
                "modrNm": "OSCU Gateway",
                "itemList": [],
            },
        )
        db.add(row)
        await db.flush()
        purchase = Purchase(
            branch_id=branch.id, external_ref="EXT-1", status="PENDING", outbox_id=row.id
        )
        db.add(purchase)
        await db.commit()
        purchase_id = purchase.id

    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/insertTrnsPurchase").mock(return_value=Response(200, json=envelope()))
        await _dispatch_pending(session_factory)

    async with session_factory() as db:
        refreshed = await db.get(Purchase, purchase_id)
        assert refreshed.status == "SENT"
