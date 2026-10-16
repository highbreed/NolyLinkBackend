"""Tests for the VSCU poller (app/workers/vscu_poller.py): iterates ACTIVE *VSCU* branches
only — skips OSCU branches (no VscuDeviceState, would just error) and non-ACTIVE ones — and a
failure on one branch doesn't stop the rest. Mirrors tests/services/test_poller.py's shape.

Deliberately does NOT use the db_session fixture — see that file's own docstring for why.
"""

import pytest
import respx
from httpx import Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.core.config import get_settings
from app.db.models import Branch, KraSyncState, VscuCallLog, VscuDeviceState
from app.workers.vscu_poller import _refresh_codes_for_all_vscu_branches

VSCU_URL_A = "http://vscu-a.test:8088"
VSCU_URL_B = "http://vscu-b.test:8088"


@pytest.fixture(autouse=True)
def enable_vscu(monkeypatch):
    # The poller builds its own VSCUClient from get_settings() (real env config) — see
    # tests/services/test_outbox_dispatcher_vscu.py's identical fixture for why this is needed.
    monkeypatch.setenv("VSCU_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def codes_envelope() -> dict:
    return {
        "resultCd": "000",
        "resultMsg": "ok",
        "resultDt": "20260901120000",
        "data": {"clsList": [{"cdCls": "04", "dtlList": [{"cd": "B", "cdNm": "16%"}]}]},
    }


async def test_vscu_poller_only_syncs_active_vscu_branches(db_engine: AsyncEngine):
    session_factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)

    async with session_factory() as setup_db:
        vscu_active = Branch(
            branch_code="VSCU-POLL-ACTIVE",
            tin="A200000001Z",
            bhf_id="00",
            cmc_key="unused",
            device_type="VSCU",
        )
        vscu_suspended = Branch(
            branch_code="VSCU-POLL-SUSPENDED",
            tin="A200000002Z",
            bhf_id="00",
            cmc_key="unused",
            device_type="VSCU",
            status="SUSPENDED",
        )
        oscu_active = Branch(
            branch_code="OSCU-POLL-ACTIVE", tin="A200000003Z", bhf_id="00", cmc_key="unused"
        )
        setup_db.add_all([vscu_active, vscu_suspended, oscu_active])
        await setup_db.flush()
        setup_db.add(
            VscuDeviceState(branch_id=vscu_active.id, base_url=VSCU_URL_A, dvc_srl_no="SN-1")
        )
        await setup_db.commit()

    try:
        with respx.mock:
            route = respx.post(f"{VSCU_URL_A}/code/selectCodes").mock(
                return_value=Response(200, json=codes_envelope())
            )
            await _refresh_codes_for_all_vscu_branches(session_factory)

        assert route.call_count == 1  # only the ACTIVE VSCU branch, not the other two

        async with session_factory() as db:
            state = await db.scalar(
                select(KraSyncState).where(
                    KraSyncState.tin == "A200000001Z", KraSyncState.resource == "codes"
                )
            )
            assert state is not None
            assert state.last_req_dt == "20260901120000"
    finally:
        async with session_factory() as db:
            await db.execute(delete(VscuCallLog).where(VscuCallLog.branch_id == vscu_active.id))
            await db.execute(
                delete(VscuDeviceState).where(VscuDeviceState.branch_id == vscu_active.id)
            )
            await db.execute(
                delete(KraSyncState).where(
                    KraSyncState.tin.in_(["A200000001Z", "A200000002Z", "A200000003Z"])
                )
            )
            await db.execute(
                delete(Branch).where(
                    Branch.id.in_([vscu_active.id, vscu_suspended.id, oscu_active.id])
                )
            )
            await db.commit()


async def test_vscu_poller_failure_on_one_branch_does_not_stop_others(db_engine: AsyncEngine):
    session_factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)

    async with session_factory() as setup_db:
        branch_a = Branch(
            branch_code="VSCU-POLL-FAIL",
            tin="A200000004Z",
            bhf_id="00",
            cmc_key="unused",
            device_type="VSCU",
        )
        branch_b = Branch(
            branch_code="VSCU-POLL-OK",
            tin="A200000005Z",
            bhf_id="00",
            cmc_key="unused",
            device_type="VSCU",
        )
        setup_db.add_all([branch_a, branch_b])
        await setup_db.flush()
        setup_db.add(VscuDeviceState(branch_id=branch_a.id, base_url=VSCU_URL_A, dvc_srl_no="SN-1"))
        setup_db.add(VscuDeviceState(branch_id=branch_b.id, base_url=VSCU_URL_B, dvc_srl_no="SN-2"))
        await setup_db.commit()

    try:
        with respx.mock:
            respx.post(f"{VSCU_URL_A}/code/selectCodes").mock(
                return_value=Response(
                    200,
                    json={"resultCd": "899", "resultMsg": "boom", "resultDt": None, "data": None},
                )
            )
            respx.post(f"{VSCU_URL_B}/code/selectCodes").mock(
                return_value=Response(200, json=codes_envelope())
            )
            await _refresh_codes_for_all_vscu_branches(session_factory)

        async with session_factory() as db:
            failed_state = await db.scalar(
                select(KraSyncState).where(
                    KraSyncState.tin == "A200000004Z", KraSyncState.resource == "codes"
                )
            )
            ok_state = await db.scalar(
                select(KraSyncState).where(
                    KraSyncState.tin == "A200000005Z", KraSyncState.resource == "codes"
                )
            )
            assert failed_state is None  # not advanced past the failure
            assert ok_state is not None
    finally:
        async with session_factory() as db:
            await db.execute(
                delete(VscuCallLog).where(VscuCallLog.branch_id.in_([branch_a.id, branch_b.id]))
            )
            await db.execute(
                delete(VscuDeviceState).where(
                    VscuDeviceState.branch_id.in_([branch_a.id, branch_b.id])
                )
            )
            await db.execute(
                delete(KraSyncState).where(KraSyncState.tin.in_(["A200000004Z", "A200000005Z"]))
            )
            await db.execute(delete(Branch).where(Branch.id.in_([branch_a.id, branch_b.id])))
            await db.commit()
