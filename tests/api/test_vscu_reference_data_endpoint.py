"""API smoke tests for /v1/vscu/{branch_id}/codes/sync and /items — confirms the
vscu_reference_data router is wired correctly end-to-end through the real FastAPI app."""

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
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def _make_initialized_branch(db_session) -> Branch:
    branch = Branch(
        branch_code="BR-REFDATA-API",
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


async def test_sync_codes_and_get_items_via_api(db_session, monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("VSCU_ENABLED", "true")
    get_settings.cache_clear()
    try:
        branch = await _make_initialized_branch(db_session)
        client = await _make_client(db_session)
        headers = {"X-Admin-Api-Key": "test-admin-key"}

        try:
            async with client as c:
                with respx.mock:
                    respx.post(f"{VSCU_URL}/code/selectCodes").mock(
                        return_value=Response(
                            200,
                            json={
                                "resultCd": "000",
                                "resultMsg": "ok",
                                "resultDt": "20260901120000",
                                "data": {
                                    "clsList": [
                                        {
                                            "cdCls": "04",
                                            "dtlList": [{"cd": "A", "cdNm": "Exempt"}],
                                        }
                                    ]
                                },
                            },
                        )
                    )
                    response = await c.post(f"/v1/vscu/{branch.id}/codes/sync", headers=headers)
                assert response.status_code == 200
                assert response.json() == {"synced": True}

                with respx.mock:
                    respx.post(f"{VSCU_URL}/items/selectItems").mock(
                        return_value=Response(
                            200,
                            json={
                                "resultCd": "000",
                                "resultMsg": "ok",
                                "resultDt": "20260901120000",
                                "data": {"itemList": [{"tin": branch.tin, "itemCd": "IT001"}]},
                            },
                        )
                    )
                    items_response = await c.get(f"/v1/vscu/{branch.id}/items", headers=headers)
                assert items_response.status_code == 200
                assert items_response.json()["data"]["itemList"][0]["itemCd"] == "IT001"
        finally:
            app.dependency_overrides.pop(get_db, None)
    finally:
        get_settings.cache_clear()


async def test_sync_codes_without_admin_key_is_forbidden(db_session):
    branch = await _make_initialized_branch(db_session)
    client = await _make_client(db_session)
    try:
        async with client as c:
            response = await c.post(f"/v1/vscu/{branch.id}/codes/sync")
        assert response.status_code == 403
    finally:
        app.dependency_overrides.pop(get_db, None)


async def test_reconcile_item_confirms_existing_item(db_session, monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("VSCU_ENABLED", "true")
    get_settings.cache_clear()
    try:
        branch = await _make_initialized_branch(db_session)
        client = await _make_client(db_session)
        headers = {"X-Admin-Api-Key": "test-admin-key"}
        try:
            async with client as c:
                with respx.mock:
                    respx.post(f"{VSCU_URL}/items/selectItems").mock(
                        return_value=Response(
                            200,
                            json={
                                "resultCd": "000",
                                "resultMsg": "ok",
                                "resultDt": "20260901120000",
                                "data": {
                                    "itemList": [
                                        {
                                            "tin": branch.tin,
                                            "itemCd": "KE2NTU0000001",
                                            "itemNm": "Bread",
                                            "itemClsCd": "50180000",
                                            "taxTyCd": "B",
                                            "dftPrc": "100.00",
                                            "useYn": "Y",
                                            "rraModYn": "N",
                                        }
                                    ]
                                },
                            },
                        )
                    )
                    response = await c.post(
                        f"/v1/vscu/{branch.id}/items/KE2NTU0000001/reconcile", headers=headers
                    )
                assert response.status_code == 200
                body = response.json()
                assert body["confirmed"] is True
                assert body["item_nm"] == "Bread"
                assert body["item_cls_cd"] == "50180000"
        finally:
            app.dependency_overrides.pop(get_db, None)
    finally:
        get_settings.cache_clear()


async def test_reconcile_item_reports_unconfirmed_when_not_found(db_session, monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("VSCU_ENABLED", "true")
    get_settings.cache_clear()
    try:
        branch = await _make_initialized_branch(db_session)
        client = await _make_client(db_session)
        headers = {"X-Admin-Api-Key": "test-admin-key"}
        try:
            async with client as c:
                with respx.mock:
                    respx.post(f"{VSCU_URL}/items/selectItems").mock(
                        return_value=Response(
                            200,
                            json={
                                "resultCd": "000",
                                "resultMsg": "ok",
                                "resultDt": "20260901120000",
                                "data": {"itemList": []},
                            },
                        )
                    )
                    response = await c.post(
                        f"/v1/vscu/{branch.id}/items/KE2NTU9999999/reconcile", headers=headers
                    )
                assert response.status_code == 200
                assert response.json()["confirmed"] is False
        finally:
            app.dependency_overrides.pop(get_db, None)
    finally:
        get_settings.cache_clear()
