"""Tests for code_cache_service (spec §7, §11): incremental sync, upsert semantics, the
lastReqDt advance-only-on-success rule, and classification resolution.
"""

import json
from pathlib import Path

import pytest
import respx
from httpx import Response

from app.core.config import Settings
from app.db.models import Branch, KraSyncState
from app.services import code_cache_service
from app.services.kra_adapter import KraAdapter, KraTransientServerError

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "kra_samples"
BASE_URL = "https://kra-sandbox.test/etims-api"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text())


@pytest.fixture
def adapter_settings() -> Settings:
    return Settings(kra_env="sandbox", kra_base_url_sbx=f"{BASE_URL}/")


@pytest.fixture
def kra(db_session, adapter_settings: Settings) -> KraAdapter:
    return KraAdapter(db=db_session, settings=adapter_settings)


@pytest.fixture
async def branch(db_session) -> Branch:
    b = Branch(branch_code="NAIROBI-HQ", tin="A123456789Z", bhf_id="00", cmc_key="c" * 255)
    db_session.add(b)
    await db_session.flush()
    return b


async def test_refresh_populates_cache_and_advances_last_req_dt(db_session, kra, branch):
    res_body = load_fixture("code_search_res")

    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/selectCodeList").mock(return_value=Response(200, json=res_body))
        await code_cache_service.refresh_code_list(db_session, kra, branch)

    codes = await code_cache_service.get_codes(db_session, "tax-type")
    assert len(codes) == 1
    assert codes[0].cd == "B"
    assert codes[0].cd_nm == "16%"

    state = await db_session.get(
        KraSyncState, {"tin": branch.tin, "bhf_id": branch.bhf_id, "resource": "codes"}
    )
    assert state is not None
    assert state.last_req_dt == res_body["resultDt"]


async def test_refresh_does_not_advance_last_req_dt_on_failure(db_session, kra, branch):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/selectCodeList").mock(
            return_value=Response(
                200,
                json={
                    "resultCd": "991",
                    "resultMsg": "server error",
                    "resultDt": "20990101000000",
                    "data": None,
                },
            )
        )
        with pytest.raises(KraTransientServerError):
            await code_cache_service.refresh_code_list(db_session, kra, branch)

    state = await db_session.get(
        KraSyncState, {"tin": branch.tin, "bhf_id": branch.bhf_id, "resource": "codes"}
    )
    assert state is None


async def test_refresh_uses_stored_last_req_dt_on_next_call(db_session, kra, branch):
    res_body = load_fixture("code_search_res")

    with respx.mock(base_url=BASE_URL) as mock:
        route = mock.post("/selectCodeList").mock(return_value=Response(200, json=res_body))
        await code_cache_service.refresh_code_list(db_session, kra, branch)
        await code_cache_service.refresh_code_list(db_session, kra, branch)

    first_call, second_call = route.calls
    assert (
        json.loads(first_call.request.content)["lastReqDt"]
        != json.loads(second_call.request.content)["lastReqDt"]
    )
    assert json.loads(second_call.request.content)["lastReqDt"] == res_body["resultDt"]


async def test_upsert_updates_existing_code_row(db_session, kra, branch):
    res_body = load_fixture("code_search_res")
    updated = json.loads(json.dumps(res_body))
    updated["data"]["clsList"][0]["dtlList"][0]["cdNm"] = "Sixteen Percent"

    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/selectCodeList").mock(side_effect=[Response(200, json=res_body)])
        await code_cache_service.refresh_code_list(db_session, kra, branch)

    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/selectCodeList").mock(side_effect=[Response(200, json=updated)])
        await code_cache_service.refresh_code_list(db_session, kra, branch)

    codes = await code_cache_service.get_codes(db_session, "tax-type")
    assert len(codes) == 1
    assert codes[0].cd_nm == "Sixteen Percent"


def test_resolve_classification_accepts_slug_and_raw_cd_cls():
    assert code_cache_service.resolve_classification("tax-type") == "04"
    assert code_cache_service.resolve_classification("99") == "99"
    assert code_cache_service.resolve_classification("not-a-known-slug") == "not-a-known-slug"


async def test_get_codes_returns_empty_list_for_unknown_classification(db_session):
    assert await code_cache_service.get_codes(db_session, "nonexistent") == []
