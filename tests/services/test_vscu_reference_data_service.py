"""Tests for VSCUReferenceDataService (app/services/vscu_reference_data_service.py):
codes/notices/import-items sync into the SAME cache tables and sync_framework resource keys
OSCU's code_cache_service/notice_service/import_item_service use, plus the uncached
customer/branch lookups.
"""

import pytest
import respx
from httpx import Response
from sqlalchemy import select

from app.core.config import Settings
from app.db.models import (
    Branch,
    ImportItemCache,
    KraCodeList,
    KraSyncState,
    NoticeCache,
    VscuDeviceState,
)
from app.integrations.vscu.client import VSCUClient
from app.services.vscu_reference_data_service import VSCUReferenceDataService

VSCU_URL = "http://vscu-branch.test:8088"


def envelope(data: dict | None = None, result_cd: str = "000") -> dict:
    return {"resultCd": result_cd, "resultMsg": "ok", "resultDt": "20260901120000", "data": data}


@pytest.fixture
def vscu_client(db_session) -> VSCUClient:
    return VSCUClient(db=db_session, settings=Settings(vscu_enabled=True))


async def _make_branch(db_session, tin: str = "A123456789Z") -> Branch:
    branch = Branch(
        branch_code="BR-REFDATA", tin=tin, bhf_id="00", cmc_key="unused", device_type="VSCU"
    )
    db_session.add(branch)
    await db_session.flush()
    db_session.add(VscuDeviceState(branch_id=branch.id, base_url=VSCU_URL, dvc_srl_no="SN-1"))
    await db_session.flush()
    return branch


async def test_sync_codes_upserts_into_shared_kra_code_lists_table(
    db_session, vscu_client: VSCUClient
):
    """Same table (kra_code_lists) and shape as OSCU's code_cache_service — codes are the same
    KRA reference data regardless of transport.
    """
    branch = await _make_branch(db_session)
    service = VSCUReferenceDataService(db_session, vscu_client)

    with respx.mock:
        respx.post(f"{VSCU_URL}/code/selectCodes").mock(
            return_value=Response(
                200,
                json=envelope(
                    data={
                        "clsList": [
                            {
                                "cdCls": "04",
                                "cdClsNm": "Tax Type",
                                "dtlList": [
                                    {"cd": "A", "cdNm": "Exempt", "srtOrd": 1, "useYn": "Y"},
                                    {"cd": "B", "cdNm": "16%", "srtOrd": 2, "useYn": "Y"},
                                ],
                            }
                        ]
                    }
                ),
            )
        )
        await service.sync_codes(branch)

    rows = (
        (await db_session.execute(select(KraCodeList).where(KraCodeList.cd_cls == "04")))
        .scalars()
        .all()
    )
    assert {r.cd for r in rows} == {"A", "B"}

    sync_state = await db_session.scalar(
        select(KraSyncState).where(
            KraSyncState.tin == branch.tin,
            KraSyncState.bhf_id == branch.bhf_id,
            KraSyncState.resource == "codes",
        )
    )
    assert sync_state is not None
    assert sync_state.last_req_dt == "20260901120000"


async def test_sync_notices_upserts_into_notice_cache(db_session, vscu_client: VSCUClient):
    branch = await _make_branch(db_session)
    service = VSCUReferenceDataService(db_session, vscu_client)

    with respx.mock:
        respx.post(f"{VSCU_URL}/notices/selectNotices").mock(
            return_value=Response(
                200,
                json=envelope(
                    data={
                        "noticeList": [
                            {
                                "noticeNo": 1,
                                "title": "System maintenance",
                                "cont": "Details",
                                "dtlUrl": "http://kra.go.ke/n/1",
                                "regrNm": "KRA",
                                "regDt": "20260901000000",
                            }
                        ]
                    }
                ),
            )
        )
        await service.sync_notices(branch)

    rows = (
        (await db_session.execute(select(NoticeCache).where(NoticeCache.branch_id == branch.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].notice_no == "1"
    assert rows[0].title == "System maintenance"


async def test_sync_import_items_upserts_into_import_item_cache(
    db_session, vscu_client: VSCUClient
):
    branch = await _make_branch(db_session)
    service = VSCUReferenceDataService(db_session, vscu_client)

    with respx.mock:
        respx.post(f"{VSCU_URL}/imports/selectImportItems").mock(
            return_value=Response(
                200,
                json=envelope(
                    data={
                        "itemList": [
                            {
                                "taskCd": "TASK1",
                                "dclDe": "20260901",
                                "itemSeq": 1,
                                "dclNo": "DCL1",
                                "hsCd": "HS1",
                                "itemNm": "Widget",
                                "orgnNatCd": "KE",
                            }
                        ]
                    }
                ),
            )
        )
        await service.sync_import_items(branch)

    rows = (
        (
            await db_session.execute(
                select(ImportItemCache).where(ImportItemCache.branch_id == branch.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].task_cd == "TASK1"
    assert rows[0].raw_data["itemNm"] == "Widget"


async def test_sync_codes_does_not_advance_watermark_on_no_result_dt(
    db_session, vscu_client: VSCUClient
):
    """A defensive edge case not seen in the decompiled source but guarded per
    sync_framework's own rule: never advance past a resultDt we don't actually have.
    """
    branch = await _make_branch(db_session)
    service = VSCUReferenceDataService(db_session, vscu_client)

    with respx.mock:
        respx.post(f"{VSCU_URL}/code/selectCodes").mock(
            return_value=Response(
                200,
                json={"resultCd": "000", "resultMsg": "ok", "resultDt": None, "data": None},
            )
        )
        await service.sync_codes(branch)

    sync_state = await db_session.scalar(
        select(KraSyncState).where(
            KraSyncState.tin == branch.tin,
            KraSyncState.bhf_id == branch.bhf_id,
            KraSyncState.resource == "codes",
        )
    )
    assert sync_state is None


async def test_lookup_customer_is_not_cached(db_session, vscu_client: VSCUClient):
    branch = await _make_branch(db_session)
    service = VSCUReferenceDataService(db_session, vscu_client)

    with respx.mock:
        respx.post(f"{VSCU_URL}/customers/selectCustomer").mock(
            return_value=Response(
                200,
                json=envelope(
                    data={
                        "custList": [
                            {"tin": "A999999999Z", "taxprNm": "Buyer Ltd", "taxprSttsCd": "01"}
                        ]
                    }
                ),
            )
        )
        res = await service.lookup_customer(branch, cust_tin="A999999999Z")

    assert res.data is not None
    assert res.data.custList[0].taxprNm == "Buyer Ltd"


async def test_select_branches_passthrough(db_session, vscu_client: VSCUClient):
    branch = await _make_branch(db_session)
    service = VSCUReferenceDataService(db_session, vscu_client)

    with respx.mock:
        respx.post(f"{VSCU_URL}/branches/selectBranches").mock(
            return_value=Response(
                200,
                json=envelope(
                    data={"bhfList": [{"tin": branch.tin, "bhfId": "00", "bhfNm": "HQ"}]}
                ),
            )
        )
        res = await service.select_branches(branch, last_req_dt="20200101000000")

    assert res.data is not None
    assert res.data.bhfList[0].bhfNm == "HQ"
