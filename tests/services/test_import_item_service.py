"""Tests for import_item_service (spec §6.5, §11, §14)."""

import json
from pathlib import Path

import pytest
import respx
from httpx import Response
from sqlalchemy import select

from app.core.config import Settings
from app.db.models import Branch, Outbox
from app.services import import_item_service
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


async def test_refresh_import_items_populates_cache(db_session, kra, branch):
    res_body = load_fixture("import_item_search_res")

    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/selectImportItemList").mock(return_value=Response(200, json=res_body))
        await import_item_service.refresh_import_items(db_session, kra, branch)

    pending = await import_item_service.get_pending_imports(db_session, branch.id)
    assert len(pending) == 1
    assert pending[0].task_cd == "T001"
    assert pending[0].impt_item_stts_cd == "1"


async def test_submit_import_decision_enqueues_and_updates_cache(db_session, kra, branch):
    res_body = load_fixture("import_item_search_res")
    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/selectImportItemList").mock(return_value=Response(200, json=res_body))
        await import_item_service.refresh_import_items(db_session, kra, branch)

    updated = await import_item_service.submit_import_decision(
        db_session,
        branch,
        task_cd="T001",
        item_cls_cd="5020150101",
        item_cd="KE1BGKG0000005",
        decision_cd="3",
    )
    assert updated.impt_item_stts_cd == "3"
    assert updated.item_cd == "KE1BGKG0000005"

    result = await db_session.execute(select(Outbox).where(Outbox.endpoint == "/updateImportItem"))
    rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].payload["imptItemsttsCd"] == "3"
    assert rows[0].payload["taskCd"] == "T001"


async def test_submit_import_decision_unknown_task_cd_raises(db_session, branch):
    with pytest.raises(import_item_service.ImportItemNotFound):
        await import_item_service.submit_import_decision(
            db_session, branch, task_cd="NOPE", item_cls_cd="x", item_cd="y", decision_cd="3"
        )
