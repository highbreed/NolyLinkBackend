"""API tests for POST /v1/vscu/{branch_id}/stock/{sar_no}/reconcile."""

import respx
from httpx import ASGITransport, AsyncClient, Response

from app.core.config import get_settings
from app.db.models import Branch, VscuDeviceState
from app.db.session import get_db
from app.main import app

VSCU_URL = "http://vscu-branch.test:8088"


async def _make_branch(db_session) -> Branch:
    branch = Branch(
        branch_code="BR-STOCK-RECON",
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


async def test_reconcile_confirms_existing_stock_move(db_session, monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("VSCU_ENABLED", "true")
    get_settings.cache_clear()
    try:

        async def _override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_get_db
        try:
            branch = await _make_branch(db_session)
            headers = {"X-Admin-Api-Key": "test-admin-key"}
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                with respx.mock:
                    respx.post(f"{VSCU_URL}/stock/selectStockItems").mock(
                        return_value=Response(
                            200,
                            json={
                                "resultCd": "000",
                                "resultMsg": "ok",
                                "resultDt": "20260901120000",
                                "data": {
                                    "stockList": [
                                        {
                                            "sarNo": 42,
                                            "ocrnDt": "20260901",
                                            "totItemCnt": 1,
                                            "totTaxblAmt": "100.00",
                                            "totTaxAmt": "16.00",
                                            "totAmt": "116.00",
                                            "itemList": [],
                                        }
                                    ]
                                },
                            },
                        )
                    )
                    response = await c.post(
                        f"/v1/vscu/{branch.id}/stock/42/reconcile", headers=headers
                    )
            assert response.status_code == 200
            body = response.json()
            assert body["confirmed"] is True
            assert body["sar_no"] == 42
            assert body["tot_amt"] == "116.00"
        finally:
            app.dependency_overrides.pop(get_db, None)
    finally:
        get_settings.cache_clear()


async def test_reconcile_reports_unconfirmed_when_not_found(db_session, monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("VSCU_ENABLED", "true")
    get_settings.cache_clear()
    try:

        async def _override_get_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_get_db
        try:
            branch = await _make_branch(db_session)
            headers = {"X-Admin-Api-Key": "test-admin-key"}
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                with respx.mock:
                    respx.post(f"{VSCU_URL}/stock/selectStockItems").mock(
                        return_value=Response(
                            200,
                            json={
                                "resultCd": "000",
                                "resultMsg": "ok",
                                "resultDt": "20260901120000",
                                "data": {"stockList": []},
                            },
                        )
                    )
                    response = await c.post(
                        f"/v1/vscu/{branch.id}/stock/999/reconcile", headers=headers
                    )
            assert response.status_code == 200
            assert response.json()["confirmed"] is False
        finally:
            app.dependency_overrides.pop(get_db, None)
    finally:
        get_settings.cache_clear()
