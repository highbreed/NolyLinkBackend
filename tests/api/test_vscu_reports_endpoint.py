"""API tests for /v1/vscu/{branch_id}/reports/z/*: the save-then-check Z-report round trip."""

import respx
from httpx import ASGITransport, AsyncClient, Response

from app.core.config import get_settings
from app.db.models import Branch, VscuDeviceState
from app.db.session import get_db
from app.main import app

VSCU_URL = "http://vscu-branch.test:8088"


async def _make_client(db_session):
    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    return ASGITransport(app=app)


async def _make_branch(db_session) -> Branch:
    branch = Branch(
        branch_code="BR-ZREPORT",
        tin="A123456789Z",
        bhf_id="00",
        cmc_key="unused",
        device_type="VSCU",
    )
    db_session.add(branch)
    await db_session.flush()
    db_session.add(VscuDeviceState(branch_id=branch.id, base_url=VSCU_URL, dvc_srl_no="SN-1"))
    await db_session.flush()
    return branch


async def test_save_and_check_z_report(db_session, monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("VSCU_ENABLED", "true")
    get_settings.cache_clear()
    try:
        branch = await _make_branch(db_session)
        transport = await _make_client(db_session)
        headers = {"X-Admin-Api-Key": "test-admin-key"}

        try:
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                with respx.mock:
                    respx.post(f"{VSCU_URL}/reports/saveZReports").mock(
                        return_value=Response(
                            200,
                            json={
                                "resultCd": "000",
                                "resultMsg": "The request has been processed successfully",
                                "resultDt": "20260901120000",
                                "data": None,
                            },
                        )
                    )
                    save_response = await c.post(
                        f"/v1/vscu/{branch.id}/reports/z/save",
                        params={"rpt_de": "20260901"},
                        headers=headers,
                    )
                assert save_response.status_code == 200
                assert save_response.json()["result_cd"] == "000"

                with respx.mock:
                    respx.post(f"{VSCU_URL}/reports/checkZReport").mock(
                        return_value=Response(
                            200,
                            json={
                                "resultCd": "000",
                                "resultMsg": "ok",
                                "resultDt": "20260901120000",
                                "data": {
                                    "checkCode": "01",
                                    "zRpt": {"rcptPbctCnt": 5, "nrmSalesAmt": "500.00"},
                                    "rcptRpt": None,
                                },
                            },
                        )
                    )
                    check_response = await c.get(
                        f"/v1/vscu/{branch.id}/reports/z/20260901", headers=headers
                    )
                assert check_response.status_code == 200
                body = check_response.json()
                assert body["data"]["checkCode"] == "01"
                assert body["data"]["zRpt"]["rcptPbctCnt"] == 5
        finally:
            app.dependency_overrides.pop(get_db, None)
    finally:
        get_settings.cache_clear()
