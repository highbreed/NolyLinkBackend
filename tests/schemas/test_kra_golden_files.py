"""Golden-file tests for the KRA Pydantic mirrors (spec §6).

The original KRA "OSCU Requirements & Communication Protocols" document (with its literal
JSON SAMPLE payloads) isn't attached to this repo — only the derived
KRA_OSCU_FastAPI_Integration_Spec.md. These fixtures are therefore constructed directly from
that spec's §6 field tables (exact field names/types/lengths), not copied verbatim from KRA's
own document. If that original document becomes available, these fixtures should be replaced
with literal excerpts from it for a stronger guarantee.

Each schema is asserted to (a) parse the fixture without validation errors, and (b) round-trip
back to an equivalent JSON structure.
"""

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from app.schemas.kra.branch import (
    BhfCustSaveReq,
    BhfCustSaveRes,
    BhfInsuranceSaveReq,
    BhfInsuranceSaveRes,
    BhfSearchReq,
    BhfSearchRes,
    BhfUserSaveReq,
    BhfUserSaveRes,
)
from app.schemas.kra.codes import (
    CodeSearchReq,
    CodeSearchRes,
    CustSearchReq,
    CustSearchRes,
    NoticeSearchReq,
    NoticeSearchRes,
)
from app.schemas.kra.device import DeviceVerificationReq, DeviceVerificationRes
from app.schemas.kra.import_item import (
    ImportItemSearchReq,
    ImportItemSearchRes,
    ImportItemUpdateReq,
    ImportItemUpdateRes,
)
from app.schemas.kra.item import (
    ItemClsSearchReq,
    ItemClsSearchRes,
    ItemSaveReq,
    ItemSaveRes,
    ItemSearchReq,
    ItemSearchRes,
)
from app.schemas.kra.purchase import (
    TrnsPurchaseSalesReq,
    TrnsPurchaseSalesRes,
    TrnsPurchaseSaveReq,
    TrnsPurchaseSaveRes,
)
from app.schemas.kra.sales import TrnsSalesSaveWrReq, TrnsSalesSaveWrRes
from app.schemas.kra.stock import (
    StockIOSaveReq,
    StockIOSaveRes,
    StockMasterSaveReq,
    StockMasterSaveRes,
    StockMoveReq,
    StockMoveRes,
)

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "kra_samples"

SCHEMA_BY_FIXTURE: dict[str, type[BaseModel]] = {
    "device_verification_req": DeviceVerificationReq,
    "device_verification_res": DeviceVerificationRes,
    "code_search_req": CodeSearchReq,
    "code_search_res": CodeSearchRes,
    "cust_search_req": CustSearchReq,
    "cust_search_res": CustSearchRes,
    "notice_search_req": NoticeSearchReq,
    "notice_search_res": NoticeSearchRes,
    "item_cls_search_req": ItemClsSearchReq,
    "item_cls_search_res": ItemClsSearchRes,
    "item_save_req": ItemSaveReq,
    "item_save_res": ItemSaveRes,
    "item_search_req": ItemSearchReq,
    "item_search_res": ItemSearchRes,
    "bhf_search_req": BhfSearchReq,
    "bhf_search_res": BhfSearchRes,
    "bhf_cust_save_req": BhfCustSaveReq,
    "bhf_cust_save_res": BhfCustSaveRes,
    "bhf_user_save_req": BhfUserSaveReq,
    "bhf_user_save_res": BhfUserSaveRes,
    "bhf_insurance_save_req": BhfInsuranceSaveReq,
    "bhf_insurance_save_res": BhfInsuranceSaveRes,
    "import_item_search_req": ImportItemSearchReq,
    "import_item_search_res": ImportItemSearchRes,
    "import_item_update_req": ImportItemUpdateReq,
    "import_item_update_res": ImportItemUpdateRes,
    "trns_sales_save_wr_req": TrnsSalesSaveWrReq,
    "trns_sales_save_wr_res": TrnsSalesSaveWrRes,
    "trns_purchase_sales_req": TrnsPurchaseSalesReq,
    "trns_purchase_sales_res": TrnsPurchaseSalesRes,
    "trns_purchase_save_req": TrnsPurchaseSaveReq,
    "trns_purchase_save_res": TrnsPurchaseSaveRes,
    "stock_move_req": StockMoveReq,
    "stock_move_res": StockMoveRes,
    "stock_io_save_req": StockIOSaveReq,
    "stock_io_save_res": StockIOSaveRes,
    "stock_master_save_req": StockMasterSaveReq,
    "stock_master_save_res": StockMasterSaveRes,
}


@pytest.mark.parametrize("fixture_name", sorted(SCHEMA_BY_FIXTURE))
def test_schema_parses_and_round_trips(fixture_name: str) -> None:
    schema = SCHEMA_BY_FIXTURE[fixture_name]
    raw = json.loads((FIXTURES_DIR / f"{fixture_name}.json").read_text())

    model = schema.model_validate(raw)
    round_tripped = model.model_dump(mode="json")

    assert round_tripped == raw


def test_every_fixture_file_is_covered() -> None:
    fixture_files = {p.stem for p in FIXTURES_DIR.glob("*.json")}
    assert fixture_files == set(SCHEMA_BY_FIXTURE)
