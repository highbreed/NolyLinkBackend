"""API tests for POST /v1/customers/onboard — the combined stepper-form flow that registers
every branch a new Customer needs (KRA-side device registration, plus local VSCU app init for
VSCU branches) and creates the Customer in one call. See
app/services/customer_onboarding_service.py's module docstring for why this is best-effort
per branch rather than all-or-nothing.
"""

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from app.core.config import get_settings
from app.db.models import Branch
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


def kra_init_res(tin: str, bhf_id: str, result_cd: str = "000") -> dict:
    return {
        "resultCd": result_cd,
        "resultMsg": "It is succeeded" if result_cd == "000" else "server error",
        "resultDt": "20260901120000",
        "data": (
            None
            if result_cd != "000"
            else {
                "info": {
                    "tin": tin,
                    "taxprNm": "ACME LTD",
                    "bsnsActv": "Retail",
                    "bhfId": bhf_id,
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
                    "dvcId": "DVC001",
                    "sdcId": "SDC001",
                    "mrcNo": "MRC001",
                    "cmcKey": "c" * 100,
                }
            }
        ),
    }


def vscu_init_res() -> dict:
    return {
        "resultCd": "000",
        "resultMsg": "The request has been processed successfully",
        "resultDt": "20260901120000",
        "data": {
            "info": {
                "tin": "P051234567X",
                "taxprNm": "ACME LTD",
                "bsnsActv": "Retail",
                "bhfId": "01",
                "bhfNm": "Mombasa Port",
                "bhfOpenDt": "20200101",
                "prvncNm": "Mombasa",
                "dstrtNm": "Mombasa",
                "sctrNm": "Port",
                "locDesc": "Port Area",
                "hqYn": "N",
                "mgrNm": "Jane Doe",
                "mgrTelNo": "0700000000",
                "mgrEmail": "jane@example.com",
                "sdcId": "SDC002",
                "mrcNo": "MRC002",
                "dvcId": "DVC002",
                "intrlKey": None,
                "signKey": None,
                "cmcKey": None,
                "lastPchsInvcNo": 0,
                "lastSaleRcptNo": 0,
                "lastInvcNo": 0,
                "lastSaleInvcNo": 0,
                "lastTrainInvcNo": 0,
                "lastProfrmInvcNo": 0,
                "lastCopyInvcNo": 0,
                "vatTyCd": "A",
            }
        },
    }


async def test_onboard_registers_multiple_oscu_branches_and_creates_customer(client):
    admin_key = get_settings().admin_api_key
    assert admin_key, "ADMIN_API_KEY must be set in the test environment's .env"
    kra_base_url = get_settings().kra_base_url

    with respx.mock(base_url=kra_base_url) as mock:
        mock.post("/selectInitOsdcInfo").mock(
            side_effect=[
                Response(200, json=kra_init_res("A123456789Z", "00")),
                Response(200, json=kra_init_res("A123456789Z", "01")),
            ]
        )
        async with client as c:
            response = await c.post(
                "/v1/customers/onboard",
                json={
                    "name": "Acme Retail Ltd",
                    "branches": [
                        {"branch_code": "ACME-HQ", "tin": "A123456789Z", "bhf_id": "00"},
                        {"branch_code": "ACME-BR2", "tin": "A123456789Z", "bhf_id": "01"},
                    ],
                },
                headers={"X-Admin-Api-Key": admin_key},
            )

    assert response.status_code == 201
    body = response.json()
    assert len(body["branches"]) == 2
    assert all(b["status"] == "REGISTERED" for b in body["branches"])
    assert sorted(body["customer"]["branch_codes"]) == ["ACME-BR2", "ACME-HQ"]


async def test_onboard_partial_failure_still_creates_customer_with_successful_branches(client):
    admin_key = get_settings().admin_api_key
    kra_base_url = get_settings().kra_base_url

    with respx.mock(base_url=kra_base_url) as mock:
        mock.post("/selectInitOsdcInfo").mock(
            side_effect=[
                Response(200, json=kra_init_res("A123456789Z", "00")),
                Response(200, json=kra_init_res("A123456789Z", "01", result_cd="991")),
            ]
        )
        async with client as c:
            response = await c.post(
                "/v1/customers/onboard",
                json={
                    "name": "Acme Retail Ltd",
                    "branches": [
                        {"branch_code": "ACME-HQ", "tin": "A123456789Z", "bhf_id": "00"},
                        {"branch_code": "ACME-BAD", "tin": "A123456789Z", "bhf_id": "01"},
                    ],
                },
                headers={"X-Admin-Api-Key": admin_key},
            )

    assert response.status_code == 201
    body = response.json()
    statuses = {b["branch_code"]: b["status"] for b in body["branches"]}
    assert statuses == {"ACME-HQ": "REGISTERED", "ACME-BAD": "FAILED"}
    failed = next(b for b in body["branches"] if b["branch_code"] == "ACME-BAD")
    assert failed["branch_id"] is None
    assert "991" in failed["error"]
    # The customer still exists, scoped to only the branch that actually registered.
    assert body["customer"]["branch_codes"] == ["ACME-HQ"]


async def test_onboard_vscu_branch_skips_kra_and_runs_local_device_init_only(
    client, monkeypatch, db_session
):
    """VSCU has no KRA-hosted registration call at all (decompiled vscu-v2.0.21.jar's
    confirmed endpoint list has no /selectInitOsdcInfo-equivalent — see
    app/services/cmc_key_manager.py's _register_vscu_branch_locally). Registering with a
    respx.mock that has NO route for the real kra_base_url proves this — the test would fail
    on an unmocked-request error if the code ever regressed to calling real KRA for VSCU.
    """
    admin_key = get_settings().admin_api_key
    kra_base_url = get_settings().kra_base_url
    vscu_local_url = "http://vscu-local.test:8088"

    monkeypatch.setenv("VSCU_ENABLED", "true")
    monkeypatch.setenv("KRA_ENV", "sandbox")
    monkeypatch.setenv("VSCU_DVC_SRL_NO_SBX", "SN-DOMAIN-1")
    get_settings.cache_clear()
    try:
        with respx.mock(base_url=kra_base_url, assert_all_called=False) as mock:
            mock.post("/selectInitOsdcInfo").mock(
                side_effect=AssertionError("must not call KRA for VSCU")
            )
            mock.post(f"{vscu_local_url}/initializer/selectInitInfo").mock(
                return_value=Response(200, json=vscu_init_res())
            )
            async with client as c:
                response = await c.post(
                    "/v1/customers/onboard",
                    json={
                        "name": "Acme Retail Ltd",
                        "branches": [
                            {
                                "branch_code": "ACME-MOMBASA",
                                "tin": "P051234567X",
                                "bhf_id": "01",
                                "device_type": "VSCU",
                                "base_url": vscu_local_url,
                            }
                        ],
                    },
                    headers={"X-Admin-Api-Key": admin_key},
                )

        assert response.status_code == 201
        body = response.json()
        assert body["branches"][0]["status"] == "REGISTERED"
        assert body["branches"][0]["device_type"] == "VSCU"

        branch = await db_session.get(Branch, body["branches"][0]["branch_id"])
        assert branch is not None
        assert branch.cmc_key is None
    finally:
        get_settings.cache_clear()


async def test_onboard_vscu_branch_reports_a_real_error_when_dvc_srl_no_unconfigured(
    client, monkeypatch
):
    """Regression test: a VSCUConfigurationError (e.g. VSCU_DVC_SRL_NO_SBX unset) has no real
    KRA resultCd/resultMsg to report — reaching into exc.context directly for that used to
    produce the useless "VSCU error None: None" instead of the actual reason (2026-09-02 fix,
    see app/services/customer_onboarding_service.py's except VSCUError branch).
    """
    admin_key = get_settings().admin_api_key
    vscu_local_url = "http://vscu-local.test:8088"

    monkeypatch.setenv("VSCU_ENABLED", "true")
    monkeypatch.setenv("KRA_ENV", "sandbox")
    # setenv to "", not delenv: pydantic-settings still reads a real .env file underneath env
    # vars, so if VSCU_DVC_SRL_NO_SBX is ever set there too, delenv alone wouldn't unset it —
    # an explicit empty env var is the only override that's guaranteed to win.
    monkeypatch.setenv("VSCU_DVC_SRL_NO_SBX", "")
    get_settings.cache_clear()
    try:
        async with client as c:
            response = await c.post(
                "/v1/customers/onboard",
                json={
                    "name": "Acme Retail Ltd",
                    "branches": [
                        {
                            "branch_code": "ACME-MOMBASA",
                            "tin": "P051234567X",
                            "bhf_id": "01",
                            "device_type": "VSCU",
                            "base_url": vscu_local_url,
                        }
                    ],
                },
                headers={"X-Admin-Api-Key": admin_key},
            )

        assert response.status_code == 201
        error = response.json()["branches"][0]["error"]
        assert error is not None
        assert "None: None" not in error
        assert "dvcSrlNo" in error or "dvc_srl_no" in error
    finally:
        get_settings.cache_clear()


async def test_onboard_rejects_vscu_branch_missing_base_url(client):
    admin_key = get_settings().admin_api_key
    async with client as c:
        response = await c.post(
            "/v1/customers/onboard",
            json={
                "name": "Acme Retail Ltd",
                "branches": [
                    {
                        "branch_code": "ACME-MOMBASA",
                        "tin": "P051234567X",
                        "bhf_id": "01",
                        "device_type": "VSCU",
                    }
                ],
            },
            headers={"X-Admin-Api-Key": admin_key},
        )
    assert response.status_code == 422


async def test_onboard_detects_cross_customer_branch_conflict(client):
    admin_key = get_settings().admin_api_key
    kra_base_url = get_settings().kra_base_url

    with respx.mock(base_url=kra_base_url) as mock:
        mock.post("/selectInitOsdcInfo").mock(
            return_value=Response(200, json=kra_init_res("A123456789Z", "00"))
        )
        async with client as c:
            first = await c.post(
                "/v1/customers/onboard",
                json={
                    "name": "First Customer",
                    "branches": [
                        {"branch_code": "SHARED-BRANCH", "tin": "A123456789Z", "bhf_id": "00"}
                    ],
                },
                headers={"X-Admin-Api-Key": admin_key},
            )
            assert first.json()["branches"][0]["status"] == "REGISTERED"

            # Second onboarding submits the SAME (tin, bhfId) under a different customer —
            # KRA's own "already installed" (902) response is what a real duplicate tin/bhfId
            # would trigger. Reassigning the same route's mock swaps its response for calls
            # from here on, within this still-open respx.mock block.
            mock.post("/selectInitOsdcInfo").mock(
                return_value=Response(
                    200,
                    json={
                        "resultCd": "902",
                        "resultMsg": "device already installed",
                        "resultDt": "20260901120000",
                        "data": None,
                    },
                )
            )
            second = await c.post(
                "/v1/customers/onboard",
                json={
                    "name": "Second Customer",
                    "branches": [
                        {"branch_code": "SHARED-BRANCH-2", "tin": "A123456789Z", "bhf_id": "00"}
                    ],
                },
                headers={"X-Admin-Api-Key": admin_key},
            )

    assert second.status_code == 201
    body = second.json()
    assert body["branches"][0]["status"] == "FAILED"
    assert "different customer" in body["branches"][0]["error"]
    assert body["customer"]["branch_codes"] == []
