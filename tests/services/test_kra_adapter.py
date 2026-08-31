"""Tests for KraAdapter (spec §5): every resultCd path from spec §10's table, using respx to
mock the KRA HTTP transport, plus call logging.
"""

import json
from pathlib import Path

import httpx
import pytest
import respx
from httpx import Response
from sqlalchemy import select

from app.core.config import Settings
from app.db.models import Branch, KraCallLog
from app.schemas.kra.codes import CodeSearchReq
from app.services.kra_adapter import (
    KraAdapter,
    KraAuthError,
    KraBusinessError,
    KraDuplicateError,
    KraRateLimitError,
    KraTransientServerError,
    KraTransportError,
    KraUnknownError,
    KraValidationError,
)

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "kra_samples"
BASE_URL = "https://kra-sandbox.test/etims-api"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text())


@pytest.fixture
def adapter_settings() -> Settings:
    return Settings(kra_env="sandbox", kra_base_url_sbx=f"{BASE_URL}/")


@pytest.fixture
def adapter(db_session, adapter_settings: Settings) -> KraAdapter:
    return KraAdapter(db=db_session, settings=adapter_settings)


def envelope(result_cd: str, result_msg: str = "msg", data: dict | None = None) -> dict:
    return {
        "resultCd": result_cd,
        "resultMsg": result_msg,
        "resultDt": "20200226143124",
        "data": data,
    }


async def test_success_returns_parsed_response(adapter: KraAdapter, db_session):
    req = CodeSearchReq.model_validate(load_fixture("code_search_req"))
    res_body = load_fixture("code_search_res")

    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/selectCodeList").mock(return_value=Response(200, json=res_body))
        result = await adapter.get_code_list(req)

    assert result.resultCd == "000"
    assert result.data is not None
    assert result.data.clsList[0].cdCls == "04"


async def test_no_search_result_does_not_raise(adapter: KraAdapter):
    req = CodeSearchReq.model_validate(load_fixture("code_search_req"))

    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/selectCodeList").mock(
            return_value=Response(200, json=envelope("001", "No search result", data=None))
        )
        result = await adapter.get_code_list(req)

    assert result.resultCd == "001"
    assert result.data is None


@pytest.mark.parametrize(
    "result_cd,expected_exc",
    [
        ("891", KraValidationError),
        ("899", KraValidationError),
        ("910", KraValidationError),
        ("912", KraValidationError),
        ("900", KraAuthError),
        ("901", KraAuthError),
        ("902", KraAuthError),
        ("903", KraAuthError),
        ("921", KraBusinessError),
        ("922", KraBusinessError),
        ("990", KraRateLimitError),
        ("991", KraTransientServerError),
        ("993", KraTransientServerError),
        ("999", KraTransientServerError),
        ("994", KraDuplicateError),
        ("995", KraUnknownError),
        ("777", KraUnknownError),
    ],
)
async def test_result_code_maps_to_expected_exception(
    adapter: KraAdapter, result_cd: str, expected_exc: type[Exception]
):
    req = CodeSearchReq.model_validate(load_fixture("code_search_req"))

    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/selectCodeList").mock(
            return_value=Response(200, json=envelope(result_cd, "some message", data=None))
        )
        with pytest.raises(expected_exc) as exc_info:
            await adapter.get_code_list(req)

    assert exc_info.value.result_cd == result_cd


async def test_every_call_is_logged_success_and_failure(adapter: KraAdapter, db_session):
    req = CodeSearchReq.model_validate(load_fixture("code_search_req"))

    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/selectCodeList").mock(
            side_effect=[
                Response(200, json=load_fixture("code_search_res")),
                Response(200, json=envelope("991", "server error", data=None)),
            ]
        )
        await adapter.get_code_list(req)
        with pytest.raises(KraTransientServerError):
            await adapter.get_code_list(req)

    rows = (await db_session.execute(select(KraCallLog).order_by(KraCallLog.id))).scalars().all()
    assert len(rows) == 2
    assert rows[0].result_cd == "000"
    assert rows[1].result_cd == "991"
    for row in rows:
        assert row.endpoint == "/selectCodeList"
        # the request carries a 255-char cmcKey; the log must never store it in plaintext
        assert "c" * 255 not in row.request_hash
        assert len(row.request_hash) == 64  # sha256 hex digest


async def test_branch_id_without_override_resolves_to_settings_default(
    adapter: KraAdapter, db_session
):
    """A branch with no kra_base_url_override (the common OSCU case) must still route through
    the DB-lookup path added for per-branch routing, landing on the same host as before.
    """
    branch = Branch(branch_code="OSCU-STD", tin="A444444444Z", bhf_id="00", cmc_key="irrelevant")
    db_session.add(branch)
    await db_session.flush()

    req = CodeSearchReq.model_validate(load_fixture("code_search_req"))
    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/selectCodeList").mock(
            return_value=Response(200, json=load_fixture("code_search_res"))
        )
        result = await adapter.get_code_list(req, branch_id=branch.id)

    assert result.resultCd == "000"


async def test_branch_kra_base_url_override_routes_call_to_that_host(
    adapter: KraAdapter, db_session
):
    """A shared KraAdapter instance (the exact pattern outbox_dispatcher.py uses to dispatch
    rows for many branches through one adapter) must route each call to that branch's own
    kra_base_url_override host, not the adapter's default. This is the multi-tenant OSCU/VSCU
    scenario — different clients on different KRA hosts, same shared dispatcher.
    """
    override_url = "https://vscu-tenant-x.example/etims-api"
    branch = Branch(
        branch_code="VSCU-TENANT-X",
        tin="A555555555Z",
        bhf_id="00",
        cmc_key="irrelevant",
        device_type="VSCU",
        kra_base_url_override=override_url,
    )
    db_session.add(branch)
    await db_session.flush()

    req = CodeSearchReq.model_validate(load_fixture("code_search_req"))
    res_body = load_fixture("code_search_res")

    with respx.mock() as mock:
        route = mock.post(f"{override_url}/selectCodeList").mock(
            return_value=Response(200, json=res_body)
        )
        result = await adapter.get_code_list(req, branch_id=branch.id)

    assert route.call_count == 1
    assert result.resultCd == "000"


async def test_http_404_from_the_kra_host_raises_kra_transport_error_not_a_raw_httpx_error(
    adapter: KraAdapter, db_session
):
    """Reproduces a real incident: a misconfigured/misrouted KRA_BASE_URL_* (wrong path, wrong
    device firmware) returns a plain HTTP 404 with no JSON envelope at all. Before this fix,
    httpx_response.raise_for_status() raised an unhandled httpx.HTTPStatusError straight
    through every caller's `except KraError` handling, surfacing as an unhandled 500 instead
    of a clean 502 (app/api/v1/endpoints/device.py and friends only ever caught KraError).
    """
    req = CodeSearchReq.model_validate(load_fixture("code_search_req"))

    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/selectCodeList").mock(return_value=Response(404, text="Not Found"))
        with pytest.raises(KraTransportError) as exc_info:
            await adapter.get_code_list(req)

    assert exc_info.value.result_cd == "HTTP_404"
    assert "404" in exc_info.value.result_msg


async def test_connection_failure_raises_kra_transport_error(adapter: KraAdapter, db_session):
    """A device that's simply unreachable (down, wrong IP, network partition) — httpx raises
    a connection-level error, not an HTTP status at all, before this fix that also escaped
    every caller's except KraError.
    """
    req = CodeSearchReq.model_validate(load_fixture("code_search_req"))

    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/selectCodeList").mock(side_effect=httpx.ConnectError("Connection refused"))
        with pytest.raises(KraTransportError) as exc_info:
            await adapter.get_code_list(req)

    assert exc_info.value.result_cd == "TRANSPORT_ERROR"
    assert "ConnectError" in exc_info.value.result_msg


async def test_transport_failure_is_still_logged_to_kra_call_log(adapter: KraAdapter, db_session):
    req = CodeSearchReq.model_validate(load_fixture("code_search_req"))

    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/selectCodeList").mock(return_value=Response(404, text="Not Found"))
        with pytest.raises(KraTransportError):
            await adapter.get_code_list(req)

    rows = (await db_session.execute(select(KraCallLog))).scalars().all()
    assert len(rows) == 1
    assert rows[0].result_cd == "HTTP_404"
