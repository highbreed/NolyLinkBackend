"""Tests for item_class_cache_service (spec §6.3): incremental sync, upsert semantics, and the
lastReqDt advance-only-on-success rule — mirrors tests/services/test_code_cache_service.py.
"""

import json
from pathlib import Path

import pytest
import respx
from httpx import Response

from app.core.config import Settings
from app.db.models import Branch, KraSyncState
from app.services import item_class_cache_service
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
    res_body = load_fixture("item_cls_search_res")

    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/selectItemClsList").mock(return_value=Response(200, json=res_body))
        await item_class_cache_service.refresh_item_classes(db_session, kra, branch)

    item_classes = await item_class_cache_service.get_item_classes(db_session)
    assert len(item_classes) == 1
    assert item_classes[0].item_cls_cd == "5020150101"
    assert item_classes[0].item_cls_nm == "Bread"
    assert item_classes[0].item_cls_lvl == "3"

    state = await db_session.get(
        KraSyncState, {"tin": branch.tin, "bhf_id": branch.bhf_id, "resource": "item_classes"}
    )
    assert state is not None
    assert state.last_req_dt == res_body["resultDt"]


async def test_refresh_does_not_advance_last_req_dt_on_failure(db_session, kra, branch):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/selectItemClsList").mock(
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
            await item_class_cache_service.refresh_item_classes(db_session, kra, branch)

    state = await db_session.get(
        KraSyncState, {"tin": branch.tin, "bhf_id": branch.bhf_id, "resource": "item_classes"}
    )
    assert state is None


async def test_upsert_updates_existing_item_class_row(db_session, kra, branch):
    res_body = load_fixture("item_cls_search_res")
    updated = json.loads(json.dumps(res_body))
    updated["data"]["itemClsList"][0]["itemClsNm"] = "Bread and Bakery"

    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/selectItemClsList").mock(side_effect=[Response(200, json=res_body)])
        await item_class_cache_service.refresh_item_classes(db_session, kra, branch)

    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/selectItemClsList").mock(side_effect=[Response(200, json=updated)])
        await item_class_cache_service.refresh_item_classes(db_session, kra, branch)

    item_classes = await item_class_cache_service.get_item_classes(db_session)
    assert len(item_classes) == 1
    assert item_classes[0].item_cls_nm == "Bread and Bakery"


async def test_get_item_classes_returns_empty_list_when_none_cached(db_session):
    assert await item_class_cache_service.get_item_classes(db_session) == []
