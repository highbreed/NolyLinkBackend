"""API tests for /v1/vscu/*: admin gate, and the health/initialize happy paths."""

import respx
from httpx import ASGITransport, AsyncClient, Response

from app.core.config import get_settings
from app.db.models import Branch
from app.db.session import get_db
from app.main import app

VSCU_URL = "http://vscu-branch.test:8088"


def _override_get_db(db_session):
    async def _inner():
        yield db_session

    return _inner


async def _make_client(db_session):
    app.dependency_overrides[get_db] = _override_get_db(db_session)
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def _make_branch(db_session) -> Branch:
    branch = Branch(
        branch_code="BR-API",
        tin="A123456789Z",
        bhf_id="00",
        cmc_key="unused",
        device_type="VSCU",
    )
    db_session.add(branch)
    await db_session.flush()
    return branch


async def test_initialize_without_admin_key_is_forbidden(db_session):
    branch = await _make_branch(db_session)
    client = await _make_client(db_session)
    try:
        async with client as c:
            response = await c.post(
                f"/v1/vscu/{branch.id}/initialize",
                json={"base_url": VSCU_URL, "dvc_srl_no": "SN-1"},
            )
        assert response.status_code == 403
    finally:
        app.dependency_overrides.pop(get_db, None)


async def test_health_for_unconfigured_branch_reports_not_configured(db_session, monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    get_settings.cache_clear()
    try:
        branch = await _make_branch(db_session)
        client = await _make_client(db_session)
        try:
            async with client as c:
                response = await c.get(
                    f"/v1/vscu/{branch.id}/health",
                    headers={"X-Admin-Api-Key": "test-admin-key"},
                )
            assert response.status_code == 200
            assert response.json()["configured"] is False
        finally:
            app.dependency_overrides.pop(get_db, None)
    finally:
        get_settings.cache_clear()


async def test_initialize_and_echo_happy_path_with_admin_key(db_session, monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("VSCU_ENABLED", "true")
    get_settings.cache_clear()
    try:
        branch = await _make_branch(db_session)
        client = await _make_client(db_session)
        headers = {"X-Admin-Api-Key": "test-admin-key"}

        init_body = {
            "resultCd": "000",
            "resultMsg": "ok",
            "resultDt": "20260901120000",
            "data": {
                "info": {
                    "tin": branch.tin,
                    "taxprNm": "ACME LTD",
                    "bsnsActv": "Retail",
                    "bhfId": branch.bhf_id,
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
                }
            },
        }

        try:
            async with client as c:
                with respx.mock:
                    respx.post(f"{VSCU_URL}/initializer/selectInitInfo").mock(
                        return_value=Response(200, json=init_body)
                    )
                    response = await c.post(
                        f"/v1/vscu/{branch.id}/initialize",
                        json={"base_url": VSCU_URL, "dvc_srl_no": "SN-1"},
                        headers=headers,
                    )
                assert response.status_code == 201
                body = response.json()
                assert body["base_url"] == VSCU_URL
                assert "cmc_key" not in body
                assert "sign_key" not in body

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
                    echo_response = await c.post(
                        f"/v1/vscu/{branch.id}/echo",
                        json={"test_str": "PING"},
                        headers=headers,
                    )
                assert echo_response.status_code == 200
                assert echo_response.json()["echoed"] == "PING"
        finally:
            app.dependency_overrides.pop(get_db, None)
    finally:
        get_settings.cache_clear()
