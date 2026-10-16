"""Tests for VSCUInitializationService/VSCUTestService (app/services/vscu_service.py): the
init -> VscuDeviceState persistence flow, the "never store cmcKey/signKey/intrlKey" guarantee,
sequence sync/update, and echo/server-time.
"""

import json

import pytest
import respx
from httpx import Response
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.db.models import Branch, VscuDeviceState
from app.integrations.vscu.client import VSCUClient
from app.integrations.vscu.exceptions import VSCUConfigurationError
from app.services.vscu_service import VSCUInitializationService, VSCUTestService

VSCU_URL = "http://vscu-branch.test:8088"


def init_envelope(result_cd: str = "000", **info_overrides) -> dict:
    info = {
        "tin": "A123456789Z",
        "taxprNm": "ACME LTD",
        "bsnsActv": "Retail",
        "bhfId": "00",
        "bhfNm": "Head Office",
        "bhfOpenDt": "20200101",
        "prvncNm": "Nairobi",
        "dstrtNm": "Nairobi",
        "sctrNm": "Westlands",
        "locDesc": "Along Waiyaki Way",
        "hqYn": "Y",
        "mgrNm": "Jane Doe",
        "mgrTelNo": "0700000000",
        "mgrEmail": "jane@example.com",
        "sdcId": "SDC001",
        "mrcNo": "MRC001",
        "dvcId": "DVC001",
        # Real KRA's own upstream response would carry these — the local VSCU app strips them
        # before we ever see them (verified against decompiled InitializeVsdcExcute). A test
        # fixture that included them would be testing the wrong contract.
        "intrlKey": None,
        "signKey": None,
        "cmcKey": None,
        "lastPchsInvcNo": 0,
        "lastSaleRcptNo": 10,
        "lastInvcNo": 5,
        "lastSaleInvcNo": 5,
        "lastTrainInvcNo": 0,
        "lastProfrmInvcNo": 0,
        "lastCopyInvcNo": 0,
        "vatTyCd": "A",
        **info_overrides,
    }
    return {
        "resultCd": result_cd,
        "resultMsg": "The request has been processed successfully",
        "resultDt": "20260901120000",
        "data": {"info": info},
    }


@pytest.fixture
def vscu_settings() -> Settings:
    return Settings(vscu_enabled=True)


@pytest.fixture
def vscu_client(db_session, vscu_settings: Settings) -> VSCUClient:
    return VSCUClient(db=db_session, settings=vscu_settings)


async def _make_branch(db_session, tin: str = "A123456789Z") -> Branch:
    branch = Branch(
        branch_code="BR-INIT", tin=tin, bhf_id="00", cmc_key="unused", device_type="VSCU"
    )
    db_session.add(branch)
    await db_session.flush()
    return branch


async def test_initialize_persists_device_state(db_session, vscu_client: VSCUClient):
    branch = await _make_branch(db_session)
    service = VSCUInitializationService(db_session, vscu_client)

    with respx.mock:
        respx.post(f"{VSCU_URL}/initializer/selectInitInfo").mock(
            return_value=Response(200, json=init_envelope())
        )
        device = await service.initialize(branch.id, base_url=VSCU_URL, dvc_srl_no="SN-100")

    assert device.base_url == VSCU_URL
    assert device.sdc_id == "SDC001"
    assert device.mrc_no == "MRC001"
    assert device.last_known_sale_rcpt_no == 10
    assert device.initialized_at is not None

    persisted = await db_session.scalar(
        select(VscuDeviceState).where(VscuDeviceState.branch_id == branch.id)
    )
    assert persisted is not None
    assert persisted.base_url == VSCU_URL


async def test_initialize_never_persists_signing_keys(db_session, vscu_client: VSCUClient):
    """Hard guarantee: even if a future/misbehaving local VSCU build ever DID leak
    cmcKey/signKey/intrlKey in its response (contrary to verified current behavior),
    VscuDeviceState has no columns to accidentally store them in.
    """
    branch = await _make_branch(db_session)
    service = VSCUInitializationService(db_session, vscu_client)

    with respx.mock:
        respx.post(f"{VSCU_URL}/initializer/selectInitInfo").mock(
            return_value=Response(
                200, json=init_envelope(cmcKey="LEAKED", signKey="LEAKED", intrlKey="LEAKED")
            )
        )
        device = await service.initialize(branch.id, base_url=VSCU_URL, dvc_srl_no="SN-100")

    device_columns = {c.name for c in VscuDeviceState.__table__.columns}
    assert "cmc_key" not in device_columns
    assert "sign_key" not in device_columns
    assert "intrl_key" not in device_columns
    assert device.sdc_id == "SDC001"  # the call still succeeded and persisted normal fields


async def test_initialize_is_reentrant_upsert(db_session, vscu_client: VSCUClient):
    """Re-running initialize (e.g. the client moved their VSCU device to a new local IP) must
    update the existing row, not create a duplicate — VscuDeviceState.branch_id is unique.
    """
    branch = await _make_branch(db_session)
    service = VSCUInitializationService(db_session, vscu_client)

    with respx.mock:
        respx.post(f"{VSCU_URL}/initializer/selectInitInfo").mock(
            return_value=Response(200, json=init_envelope())
        )
        await service.initialize(branch.id, base_url=VSCU_URL, dvc_srl_no="SN-100")

    new_url = "http://vscu-branch-new-ip.test:8088"
    with respx.mock:
        respx.post(f"{new_url}/initializer/selectInitInfo").mock(
            return_value=Response(200, json=init_envelope())
        )
        await service.initialize(branch.id, base_url=new_url, dvc_srl_no="SN-100")

    rows = (
        (
            await db_session.execute(
                select(VscuDeviceState).where(VscuDeviceState.branch_id == branch.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].base_url == new_url


async def test_initialize_falls_back_to_configured_dvc_srl_no(
    db_session, vscu_client: VSCUClient, monkeypatch
):
    """dvcSrlNo for VSCU is issued once, domain-wide, by KRA to this integration — not
    per-branch — so omitting it must resolve from Settings.vscu_dvc_srl_no_for_env(), not
    raise, and the resolved value must be what's actually sent on the wire.
    """
    monkeypatch.setenv("KRA_ENV", "sandbox")
    monkeypatch.setenv("VSCU_DVC_SRL_NO_SBX", "SN-DOMAIN-CONFIGURED")
    get_settings.cache_clear()
    try:
        branch = await _make_branch(db_session)
        service = VSCUInitializationService(db_session, vscu_client)

        with respx.mock:
            route = respx.post(f"{VSCU_URL}/initializer/selectInitInfo").mock(
                return_value=Response(200, json=init_envelope())
            )
            device = await service.initialize(branch.id, base_url=VSCU_URL)

        assert device.dvc_srl_no == "SN-DOMAIN-CONFIGURED"
        sent_body = json.loads(route.calls.last.request.content)
        assert sent_body["dvcSrlNo"] == "SN-DOMAIN-CONFIGURED"
    finally:
        get_settings.cache_clear()


async def test_initialize_without_dvc_srl_no_or_config_raises_configuration_error(
    db_session, vscu_client: VSCUClient, monkeypatch
):
    monkeypatch.setenv("KRA_ENV", "sandbox")
    # setenv to "", not delenv: pydantic-settings still reads a real .env file underneath env
    # vars, so if VSCU_DVC_SRL_NO_SBX is ever set there too, delenv alone wouldn't unset it —
    # an explicit empty env var is the only override that's guaranteed to win.
    monkeypatch.setenv("VSCU_DVC_SRL_NO_SBX", "")
    get_settings.cache_clear()
    try:
        branch = await _make_branch(db_session)
        service = VSCUInitializationService(db_session, vscu_client)

        with pytest.raises(VSCUConfigurationError):
            await service.initialize(branch.id, base_url=VSCU_URL)
    finally:
        get_settings.cache_clear()


async def test_initialize_recovers_from_already_installed_via_select_sequences(
    db_session, vscu_client: VSCUClient
):
    """A real device confirmed (2026-09-02): selectInitInfo is not re-callable on a (tin,
    bhfId) that already completed initialization — it responds 902 "This device is installed"
    instead of repeating the InitInfoVO payload. initialize() must recover by fetching the
    same data via selectSequences rather than surfacing that as a failure.
    """
    branch = await _make_branch(db_session)
    service = VSCUInitializationService(db_session, vscu_client)

    with respx.mock:
        respx.post(f"{VSCU_URL}/initializer/selectInitInfo").mock(
            return_value=Response(
                200,
                json={
                    "resultCd": "902",
                    "resultMsg": "This device is installed",
                    "resultDt": "20260901120000",
                    "data": None,
                },
            )
        )
        respx.post(f"{VSCU_URL}/initializer/selectSequences").mock(
            return_value=Response(200, json=init_envelope(sdcId="SDC-EXISTING"))
        )
        device = await service.initialize(branch.id, base_url=VSCU_URL, dvc_srl_no="SN-100")

    assert device.sdc_id == "SDC-EXISTING"
    assert device.base_url == VSCU_URL
    assert device.initialized_at is not None


async def test_initialize_unknown_branch_raises_value_error(db_session, vscu_client: VSCUClient):
    service = VSCUInitializationService(db_session, vscu_client)
    with pytest.raises(ValueError):
        await service.initialize(
            branch_id=__import__("uuid").uuid4(), base_url=VSCU_URL, dvc_srl_no="SN-1"
        )


async def test_sync_sequences_updates_cache_without_touching_base_url(
    db_session, vscu_client: VSCUClient
):
    branch = await _make_branch(db_session)
    init_service = VSCUInitializationService(db_session, vscu_client)
    with respx.mock:
        respx.post(f"{VSCU_URL}/initializer/selectInitInfo").mock(
            return_value=Response(200, json=init_envelope())
        )
        await init_service.initialize(branch.id, base_url=VSCU_URL, dvc_srl_no="SN-100")

    with respx.mock:
        respx.post(f"{VSCU_URL}/initializer/selectSequences").mock(
            return_value=Response(200, json=init_envelope(lastSaleRcptNo=42, lastInvcNo=41))
        )
        res = await init_service.sync_sequences(branch.id)

    assert res.resultCd == "000"
    device = await db_session.scalar(
        select(VscuDeviceState).where(VscuDeviceState.branch_id == branch.id)
    )
    assert device.last_known_sale_rcpt_no == 42
    assert device.last_sequence_sync_at is not None
    assert device.base_url == VSCU_URL  # unaffected by a sequences call


async def test_update_sequences_before_initialize_raises_configuration_error(
    db_session, vscu_client: VSCUClient
):
    branch = await _make_branch(db_session)
    service = VSCUInitializationService(db_session, vscu_client)
    with pytest.raises(VSCUConfigurationError):
        await service.update_sequences(branch.id)


async def test_echo_round_trips_test_string(db_session, vscu_client: VSCUClient):
    branch = await _make_branch(db_session)
    init_service = VSCUInitializationService(db_session, vscu_client)
    with respx.mock:
        respx.post(f"{VSCU_URL}/initializer/selectInitInfo").mock(
            return_value=Response(200, json=init_envelope())
        )
        await init_service.initialize(branch.id, base_url=VSCU_URL, dvc_srl_no="SN-100")

    test_service = VSCUTestService(db_session, vscu_client)
    with respx.mock:
        respx.post(f"{VSCU_URL}/test/echoTest").mock(
            return_value=Response(
                200,
                json={
                    "resultCd": "000",
                    "resultMsg": "ok",
                    "resultDt": "20260901120000",
                    "data": {"testStr": "PING"},
                },
            )
        )
        res = await test_service.echo(branch.id, test_str="PING")

    assert res.data is not None
    assert res.data.testStr == "PING"
