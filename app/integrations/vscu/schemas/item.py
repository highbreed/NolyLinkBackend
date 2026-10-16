"""POST /items/* — verified against decompiled ItemReq/ItemRes/Item, ItemSaveReq/Res,
ItemCompositionSaveReq/Res (com.kra.vsdc.execute.model, vscu-v2.0.21.jar).

IMPORTANT verified quirk: ItemExcute.saveItem does NOT propagate real KRA's resultCd to us.
After calling upstream, it unconditionally overwrites the response to resultCd="000" (queuing
a local resend on 894/896 first) — the only way this endpoint ever reports non-success to us
is if the HTTP call to KRA itself throws (network failure), which yields "899". A genuine
KRA-side business validation error on item save is silently swallowed by the local VSCU app.
Consequence: resultCd=="000" from save_item() only proves the local app accepted and queued
the request, never that real KRA actually approved it — do not treat it as fiscal-grade proof
the way /trnsSales/saveSales's resultCd can be (that endpoint DOES propagate KRA's real code).
"""

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from app.integrations.vscu.schemas.envelope import VSCURequestBase, VSCUResponseEnvelope


class ItemReq(VSCURequestBase):
    lastReqDt: str


class Item(BaseModel):
    tin: str
    itemCd: str
    itemClsCd: str | None = None
    itemTyCd: str | None = None
    itemNm: str | None = None
    itemStdNm: str | None = None
    orgnNatCd: str | None = None
    pkgUnitCd: str | None = None
    qtyUnitCd: str | None = None
    taxTyCd: str | None = None
    btchNo: str | None = None
    regBhfId: str | None = None
    bcd: str | None = None
    dftPrc: Decimal | None = Field(default=None, max_digits=18, decimal_places=2)
    grpPrcL1: Decimal | None = Field(default=None, max_digits=18, decimal_places=2)
    grpPrcL2: Decimal | None = Field(default=None, max_digits=18, decimal_places=2)
    grpPrcL3: Decimal | None = Field(default=None, max_digits=18, decimal_places=2)
    grpPrcL4: Decimal | None = Field(default=None, max_digits=18, decimal_places=2)
    grpPrcL5: Decimal | None = Field(default=None, max_digits=18, decimal_places=2)
    addInfo: str | None = None
    sftyQty: Decimal | None = Field(default=None, max_digits=18, decimal_places=2)
    isrcAplcbYn: str | None = None
    rraModYn: str | None = None
    useYn: str | None = None


class ItemResData(BaseModel):
    itemList: list[Item] = []


class ItemRes(VSCUResponseEnvelope[ItemResData]):
    pass


class ItemSaveReq(VSCURequestBase):
    itemCd: str
    itemClsCd: str
    itemTyCd: str
    itemNm: str
    itemStdNm: str | None = None
    orgnNatCd: str
    pkgUnitCd: str
    qtyUnitCd: str
    taxTyCd: str
    btchNo: str | None = None
    bcd: str | None = None
    dftPrc: Decimal = Field(max_digits=18, decimal_places=2)
    grpPrcL1: Decimal | None = Field(default=None, max_digits=18, decimal_places=2)
    grpPrcL2: Decimal | None = Field(default=None, max_digits=18, decimal_places=2)
    grpPrcL3: Decimal | None = Field(default=None, max_digits=18, decimal_places=2)
    grpPrcL4: Decimal | None = Field(default=None, max_digits=18, decimal_places=2)
    grpPrcL5: Decimal | None = Field(default=None, max_digits=18, decimal_places=2)
    addInfo: str | None = None
    sftyQty: Decimal | None = Field(default=None, max_digits=18, decimal_places=2)
    isrcAplcbYn: str | None = None
    useYn: str | None = None
    regrId: str
    regrNm: str
    modrId: str
    modrNm: str


class ItemSaveRes(VSCUResponseEnvelope[dict[str, Any]]):
    pass


class ItemCompositionSaveReq(VSCURequestBase):
    itemCd: str
    cpstItemCd: str
    cpstQty: Decimal = Field(max_digits=18, decimal_places=2)
    regrId: str
    regrNm: str


class ItemCompositionSaveRes(VSCUResponseEnvelope[dict[str, Any]]):
    pass
