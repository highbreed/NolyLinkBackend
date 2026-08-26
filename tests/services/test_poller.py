"""Tests for the code-list poller (spec §9, §11): iterates ACTIVE branches, skips others,
and a failure on one branch doesn't stop the rest.

Deliberately does NOT use the db_session fixture: the poller opens its own session per run
(as it would against Celery in production), which needs to see committed data through a
session factory bound to the same engine — a savepoint-scoped fixture session wouldn't be
visible to it. session_factory is injected precisely so this never falls back to the real
app DATABASE_URL (see app/workers/poller.py).
"""

import json
from pathlib import Path

import respx
from httpx import Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.config import get_settings
from app.db.models import Branch, KraCallLog, KraSyncState
from app.workers.poller import _refresh_code_lists_for_all_branches

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "kra_samples"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text())


async def test_poller_syncs_active_branches_and_skips_others(db_engine: AsyncEngine):
    session_factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)

    async with session_factory() as setup_db:
        active = Branch(branch_code="ACTIVE-01", tin="A100000001Z", bhf_id="00", cmc_key="c" * 10)
        inactive = Branch(
            branch_code="SUSPENDED-01",
            tin="A100000002Z",
            bhf_id="00",
            cmc_key="c" * 10,
            status="SUSPENDED",
        )
        setup_db.add_all([active, inactive])
        await setup_db.commit()

    kra_base_url = get_settings().kra_base_url
    res_body = load_fixture("code_search_res")

    try:
        with respx.mock(base_url=kra_base_url) as mock:
            route = mock.post("/selectCodeList").mock(return_value=Response(200, json=res_body))
            await _refresh_code_lists_for_all_branches(session_factory)

        assert route.call_count == 1

        async with session_factory() as check_db:
            active_state = await check_db.scalar(
                select(KraSyncState).where(KraSyncState.tin == "A100000001Z")
            )
            assert active_state is not None

            inactive_state = await check_db.scalar(
                select(KraSyncState).where(KraSyncState.tin == "A100000002Z")
            )
            assert inactive_state is None
    finally:
        async with session_factory() as cleanup_db:
            await cleanup_db.execute(
                delete(KraSyncState).where(KraSyncState.tin.in_(["A100000001Z", "A100000002Z"]))
            )
            await cleanup_db.execute(
                delete(KraCallLog).where(KraCallLog.branch_id.in_([active.id, inactive.id]))
            )
            await cleanup_db.execute(delete(Branch).where(Branch.id.in_([active.id, inactive.id])))
            await cleanup_db.commit()
