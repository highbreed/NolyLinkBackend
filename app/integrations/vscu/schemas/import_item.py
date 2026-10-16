"""POST /imports/* — verified against decompiled ImportItemReq/Res/ImportItem and
ImportItemUpdateReq/Res (com.kra.vsdc.execute.model). Both are plain proxies to real KRA, no
local logic.
"""

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from app.integrations.vscu.schemas.envelope import VSCURequestBase, VSCUResponseEnvelope


class ImportItemReq(VSCURequestBase):
    lastReqDt: str


class ImportItem(BaseModel):
    taskCd: str
    dclDe: str
    itemSeq: int
    dclNo: str | None = None
    hsCd: str | None = None
    itemNm: str | None = None
    imptItemsttsCd: str | None = None
    orgnNatCd: str | None = None
    exptNatCd: str | None = None
    pkg: Decimal | None = Field(default=None, max_digits=13, decimal_places=2)
    pkgUnitCd: str | None = None
    qty: Decimal | None = Field(default=None, max_digits=13, decimal_places=2)
    qtyUnitCd: str | None = None
    totWt: Decimal | None = Field(default=None, max_digits=18, decimal_places=2)
    netWt: Decimal | None = Field(default=None, max_digits=18, decimal_places=2)
    spplrNm: str | None = None
    agntNm: str | None = None
    invcFcurAmt: Decimal | None = Field(default=None, max_digits=18, decimal_places=2)
    invcFcurCd: str | None = None
    invcFcurExcrt: Decimal | None = Field(default=None, max_digits=18, decimal_places=4)


class ImportItemResData(BaseModel):
    itemList: list[ImportItem] = []


class ImportItemRes(VSCUResponseEnvelope[ImportItemResData]):
    pass


class ImportItemUpdateReq(VSCURequestBase):
    taskCd: str
    dclDe: str
    itemSeq: int
    hsCd: str
    itemClsCd: str
    itemCd: str
    imptItemSttsCd: str
    remark: str | None = None
    modrId: str
    modrNm: str


class ImportItemUpdateRes(VSCUResponseEnvelope[dict[str, Any]]):
    pass
