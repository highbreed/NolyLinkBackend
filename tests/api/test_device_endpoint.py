"""API tests for POST /v1/devices/initialize (spec §12, §18): admin gate, and the hard
guarantee that cmcKey never appears in a response body or log line.
"""

import json
import logging
from pathlib import Path

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from app.core.config import get_settings
from app.db.session import get_db
from app.main import app

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "kra_samples"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text())


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


async def test_initialize_without_admin_key_is_forbidden(client):
    async with client as c:
        response = await c.post(
            "/v1/devices/initialize",
            json={"branch_code": "NAIROBI-HQ", "tin": "A123456789Z", "bhf_id": "00"},
        )
    assert response.status_code == 403


async def test_initialize_with_wrong_admin_key_is_forbidden(client):
    async with client as c:
        response = await c.post(
            "/v1/devices/initialize",
            json={"branch_code": "NAIROBI-HQ", "tin": "A123456789Z", "bhf_id": "00"},
            headers={"X-Admin-Api-Key": "wrong"},
        )
    assert response.status_code == 403


async def test_initialize_success_never_leaks_cmc_key(client, caplog):
    admin_key = get_settings().admin_api_key
    assert admin_key, "ADMIN_API_KEY must be set in the test environment's .env"

    res_body = load_fixture("device_verification_res")
    plaintext_cmc_key = res_body["data"]["device"]["cmcKey"]
    kra_base_url = get_settings().kra_base_url

    with caplog.at_level(logging.INFO), respx.mock(base_url=kra_base_url) as mock:
        mock.post("/selectInitOsdcInfo").mock(return_value=Response(200, json=res_body))
        async with client as c:
            response = await c.post(
                "/v1/devices/initialize",
                json={"branch_code": "NAIROBI-HQ", "tin": "A123456789Z", "bhf_id": "00"},
                headers={"X-Admin-Api-Key": admin_key},
            )

    assert response.status_code == 201
    body = response.json()
    assert "cmc_key" not in body
    assert "cmcKey" not in body

    # Scan the raw response body and every captured log line for the literal cmcKey string —
    # not just the known field names, in case it leaked somewhere unexpected.
    assert plaintext_cmc_key not in response.text
    for record in caplog.records:
        assert plaintext_cmc_key not in record.getMessage()


async def test_kra_host_returning_404_is_a_clean_502_not_an_unhandled_500(client):
    """Reproduces a real incident: KRA_BASE_URL_SBX pointed at a real device whose
    /selectInitOsdcInfo path 404'd — an HTTP-transport-level failure with no KRA JSON
    envelope at all, which used to escape as an unhandled 500 (see
    app/services/kra_adapter.py's KraTransportError).
    """
    admin_key = get_settings().admin_api_key
    assert admin_key, "ADMIN_API_KEY must be set in the test environment's .env"
    kra_base_url = get_settings().kra_base_url

    with respx.mock(base_url=kra_base_url) as mock:
        mock.post("/selectInitOsdcInfo").mock(return_value=Response(404, text="Not Found"))
        async with client as c:
            response = await c.post(
                "/v1/devices/initialize",
                json={"branch_code": "NAIROBI-HQ", "tin": "A123456789Z", "bhf_id": "00"},
                headers={"X-Admin-Api-Key": admin_key},
            )

    assert response.status_code == 502
    assert "404" in response.json()["detail"]
