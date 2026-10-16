"""Tests for VSCUClient: per-branch base URL resolution (multi-tenant — each branch's VSCU
device is a different host, unlike the single shared KRA host for OSCU), resultCd -> exception
mapping, transport-failure classification, and call logging (app/integrations/vscu/client.py).
"""

import uuid

import httpx
import pytest
import respx
from httpx import Response
from sqlalchemy import select

from app.core.config import Settings
from app.db.models import Branch, VscuCallLog, VscuDeviceState
from app.integrations.vscu.client import VSCUClient
from app.integrations.vscu.exceptions import (
    VSCUConfigurationError,
    VSCUDuplicateSubmissionError,
    VSCUSequenceError,
    VSCUTransportError,
    VSCUUnavailableError,
    VSCUUnknownError,
    VSCUValidationError,
)
from app.integrations.vscu.schemas.test import EchoTestReq

BRANCH_A_URL = "http://vscu-tenant-a.test:8088"
BRANCH_B_URL = "http://vscu-tenant-b.test:8088"


def envelope(result_cd: str, result_msg: str = "msg", data: dict | None = None) -> dict:
    return {
        "resultCd": result_cd,
        "resultMsg": result_msg,
        "resultDt": "20260901120000",
        "data": data,
    }


@pytest.fixture
def vscu_settings() -> Settings:
    return Settings(vscu_enabled=True)


@pytest.fixture
def client(db_session, vscu_settings: Settings) -> VSCUClient:
    return VSCUClient(db=db_session, settings=vscu_settings)


async def _make_branch_with_device(db_session, base_url: str, tin: str = "A123456789Z") -> Branch:
    branch = Branch(
        branch_code=f"BR-{uuid.uuid4().hex[:8]}",
        tin=tin,
        bhf_id="00",
        cmc_key="unused-for-vscu",
        device_type="VSCU",
    )
    db_session.add(branch)
    await db_session.flush()
    db_session.add(VscuDeviceState(branch_id=branch.id, base_url=base_url, dvc_srl_no="SN-1"))
    await db_session.flush()
    return branch


async def test_disabled_raises_configuration_error(db_session):
    client = VSCUClient(db=db_session, settings=Settings(vscu_enabled=False))
    branch = await _make_branch_with_device(db_session, BRANCH_A_URL)
    req = EchoTestReq(tin=branch.tin, bhfId=branch.bhf_id, testStr="PING")
    with pytest.raises(VSCUConfigurationError):
        await client.echo_test(branch.id, req)


async def test_no_device_state_raises_configuration_error(client: VSCUClient, db_session):
    branch = Branch(
        branch_code="BR-NODEV", tin="A987654321Z", bhf_id="01", cmc_key="x", device_type="VSCU"
    )
    db_session.add(branch)
    await db_session.flush()
    req = EchoTestReq(tin=branch.tin, bhfId=branch.bhf_id, testStr="PING")
    with pytest.raises(VSCUConfigurationError):
        await client.echo_test(branch.id, req)


async def test_each_branch_resolves_its_own_host(client: VSCUClient, db_session):
    """The multi-tenant case: two branches, two entirely different local VSCU hosts — this
    must never share/confuse state the way a single global base_url would.
    """
    branch_a = await _make_branch_with_device(db_session, BRANCH_A_URL, tin="A111111111Z")
    branch_b = await _make_branch_with_device(db_session, BRANCH_B_URL, tin="A222222222Z")

    with respx.mock:
        respx.post(f"{BRANCH_A_URL}/test/echoTest").mock(
            return_value=Response(200, json=envelope("000", data={"testStr": "FROM-A"}))
        )
        respx.post(f"{BRANCH_B_URL}/test/echoTest").mock(
            return_value=Response(200, json=envelope("000", data={"testStr": "FROM-B"}))
        )
        res_a = await client.echo_test(
            branch_a.id, EchoTestReq(tin=branch_a.tin, bhfId="00", testStr="PING")
        )
        res_b = await client.echo_test(
            branch_b.id, EchoTestReq(tin=branch_b.tin, bhfId="00", testStr="PING")
        )

    assert res_a.data is not None and res_a.data.testStr == "FROM-A"
    assert res_b.data is not None and res_b.data.testStr == "FROM-B"


async def test_success_logs_call(client: VSCUClient, db_session):
    branch = await _make_branch_with_device(db_session, BRANCH_A_URL)
    with respx.mock:
        respx.post(f"{BRANCH_A_URL}/test/echoTest").mock(
            return_value=Response(200, json=envelope("000", data={"testStr": "PING"}))
        )
        await client.echo_test(branch.id, EchoTestReq(tin=branch.tin, bhfId="00", testStr="PING"))

    logs = (
        (await db_session.execute(select(VscuCallLog).where(VscuCallLog.branch_id == branch.id)))
        .scalars()
        .all()
    )
    assert len(logs) == 1
    assert logs[0].result_cd == "000"
    assert logs[0].endpoint == "/test/echoTest"


@pytest.mark.parametrize(
    "result_cd,expected_exc",
    [
        ("836", VSCUSequenceError),
        ("924", VSCUDuplicateSubmissionError),
        ("894", VSCUUnavailableError),
        ("896", VSCUUnavailableError),
        ("899", VSCUValidationError),
        ("834", VSCUValidationError),
        ("802", VSCUValidationError),
        ("891", VSCUValidationError),  # real-KRA-passthrough validation range
        ("900", VSCUValidationError),  # real-KRA-passthrough auth range
        ("921", VSCUValidationError),  # real-KRA-passthrough business range
        ("990", VSCUUnavailableError),  # real-KRA-passthrough rate limit
        ("991", VSCUUnavailableError),  # real-KRA-passthrough transient server error
        ("994", VSCUDuplicateSubmissionError),  # real-KRA-passthrough duplicate
        ("777", VSCUUnknownError),  # not in any known table (and NOT the disproven 824/838)
    ],
)
async def test_result_code_classification(
    client: VSCUClient, db_session, result_cd: str, expected_exc: type[Exception]
):
    branch = await _make_branch_with_device(db_session, BRANCH_A_URL)
    with respx.mock:
        respx.post(f"{BRANCH_A_URL}/test/echoTest").mock(
            return_value=Response(200, json=envelope(result_cd))
        )
        with pytest.raises(expected_exc):
            await client.echo_test(
                branch.id, EchoTestReq(tin=branch.tin, bhfId="00", testStr="PING")
            )


async def test_transport_failure_raises_and_logs(client: VSCUClient, db_session):
    branch = await _make_branch_with_device(db_session, BRANCH_A_URL)
    with respx.mock:
        respx.post(f"{BRANCH_A_URL}/test/echoTest").mock(side_effect=httpx.ConnectError("refused"))
        with pytest.raises(VSCUTransportError) as exc_info:
            await client.echo_test(
                branch.id, EchoTestReq(tin=branch.tin, bhfId="00", testStr="PING")
            )
    assert exc_info.value.retryable is True

    logs = (
        (await db_session.execute(select(VscuCallLog).where(VscuCallLog.branch_id == branch.id)))
        .scalars()
        .all()
    )
    assert logs[0].result_cd == "TRANSPORT_ERROR"


async def test_http_5xx_raises_transport_error(client: VSCUClient, db_session):
    branch = await _make_branch_with_device(db_session, BRANCH_A_URL)
    with respx.mock:
        respx.post(f"{BRANCH_A_URL}/test/echoTest").mock(return_value=Response(500, text="boom"))
        with pytest.raises(VSCUTransportError) as exc_info:
            await client.echo_test(
                branch.id, EchoTestReq(tin=branch.tin, bhfId="00", testStr="PING")
            )
    assert exc_info.value.context.result_cd == "HTTP_500"


async def test_server_time_returns_raw_string_not_json(client: VSCUClient, db_session):
    """Verified against decompiled MainExcute.java: GET /main/selectServerTime returns a bare
    string, not a JSON envelope.
    """
    branch = await _make_branch_with_device(db_session, BRANCH_A_URL)
    with respx.mock:
        respx.get(f"{BRANCH_A_URL}/main/selectServerTime").mock(
            return_value=Response(200, text="20260901120000")
        )
        result = await client.get_server_time(branch.id)
    assert result == "20260901120000"


async def test_per_branch_timeout_override_is_used(db_session, vscu_settings: Settings):
    """A branch's own VscuDeviceState.connect/read_timeout_seconds must win over the global
    Settings default — different clients' networks may need different timeouts.
    """
    client = VSCUClient(db=db_session, settings=vscu_settings)
    branch = Branch(
        branch_code="BR-TO", tin="A333333333Z", bhf_id="00", cmc_key="x", device_type="VSCU"
    )
    db_session.add(branch)
    await db_session.flush()
    db_session.add(
        VscuDeviceState(
            branch_id=branch.id,
            base_url=BRANCH_A_URL,
            dvc_srl_no="SN-2",
            connect_timeout_seconds=1.5,
            read_timeout_seconds=2.5,
        )
    )
    await db_session.flush()

    device = await client._resolve_device(branch.id)
    assert device.connect_timeout_seconds == 1.5
    assert device.read_timeout_seconds == 2.5
