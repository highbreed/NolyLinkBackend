"""Tests for notice_service (spec §6.2, §11)."""

import json
from pathlib import Path

import pytest
import respx
from httpx import Response

from app.core.config import Settings
from app.db.models import Branch
from app.services import notice_service
from app.services.kra_adapter import KraAdapter

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "kra_samples"
BASE_URL = "https://kra-sandbox.test/etims-api"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text())


@pytest.fixture
def kra(db_session) -> KraAdapter:
    return KraAdapter(
        db=db_session, settings=Settings(kra_env="sandbox", kra_base_url_sbx=f"{BASE_URL}/")
    )


@pytest.fixture
async def branch(db_session) -> Branch:
    b = Branch(branch_code="NAIROBI-HQ", tin="A123456789Z", bhf_id="00", cmc_key="c" * 10)
    db_session.add(b)
    await db_session.flush()
    return b


async def test_refresh_notices_populates_cache(db_session, kra, branch):
    res_body = load_fixture("notice_search_res")

    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/selectNoticeList").mock(return_value=Response(200, json=res_body))
        await notice_service.refresh_notices(db_session, kra, branch)

    notices = await notice_service.get_notices(db_session, branch.id)
    assert len(notices) == 1
    assert notices[0].title == "System maintenance"


async def test_refresh_notices_upserts_on_second_sync(db_session, kra, branch):
    res_body = load_fixture("notice_search_res")
    updated = json.loads(json.dumps(res_body))
    updated["data"]["noticeList"][0]["title"] = "Maintenance rescheduled"

    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/selectNoticeList").mock(side_effect=[Response(200, json=res_body)])
        await notice_service.refresh_notices(db_session, kra, branch)

    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/selectNoticeList").mock(side_effect=[Response(200, json=updated)])
        await notice_service.refresh_notices(db_session, kra, branch)

    notices = await notice_service.get_notices(db_session, branch.id)
    assert len(notices) == 1
    assert notices[0].title == "Maintenance rescheduled"
