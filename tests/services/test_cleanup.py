"""Tests for app/workers/cleanup.py's client_call_log retention sweep."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.db.models import ClientCallLog
from app.workers.cleanup import _cleanup_client_call_log


@pytest.fixture
def session_factory(db_engine: AsyncEngine):
    return async_sessionmaker(bind=db_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _clear_client_call_log(session_factory):
    # This file commits directly against the session-scoped db_engine (like
    # test_outbox_dispatcher.py), bypassing db_session's rollback isolation — explicit
    # cleanup avoids leaking rows into other tests sharing that same engine.
    yield
    async with session_factory() as db:
        await db.execute(delete(ClientCallLog))
        await db.commit()


async def test_deletes_only_rows_older_than_retention(session_factory):
    # Default client_call_log_retention_days is 30 (app/core/config.py) — exercised as-is,
    # no override needed: "old" below is backdated past that, "recent" is not.
    now = datetime.now(UTC)
    async with session_factory() as db:
        old_row = ClientCallLog(
            request_id="old",
            method="GET",
            path="/v1/health",
            status_code=200,
            duration_ms=1,
        )
        recent_row = ClientCallLog(
            request_id="recent",
            method="GET",
            path="/v1/health",
            status_code=200,
            duration_ms=1,
        )
        db.add_all([old_row, recent_row])
        await db.commit()
        # created_at has a server_default of now() — backdate the "old" row directly so the
        # cutoff comparison has something real to filter on.
        old_row.created_at = now - timedelta(days=31)
        db.add(old_row)
        await db.commit()

    deleted = await _cleanup_client_call_log(session_factory=session_factory)
    assert deleted == 1

    async with session_factory() as db:
        remaining = (await db.execute(select(ClientCallLog))).scalars().all()
    assert [row.request_id for row in remaining] == ["recent"]
