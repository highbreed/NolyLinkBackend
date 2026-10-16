"""POST /stock/*, POST /stockMaster/saveStockMaster — verified against decompiled
StockMoveReq/Res, StockIoItemSaveReq, StockIoSaveReq/Res, StockMasterSaveReq/Res
(com.kra.vsdc.execute.model, vscu-v2.0.21.jar).

IMPORTANT verified quirk, same as /items/saveItems (app/integrations/vscu/schemas/item.py):
StockIoExcute.saveStockItems and StockMasterExcute.saveStockMaster BOTH unconditionally
overwrite their response to resultCd="000" after queuing a local resend on 894/896 — a
genuine KRA-side rejection of either is silently swallowed by the local app. Only a transport
exception (899) or the local app's own request-handling failure ever surfaces to us here.
/stock/selectStockItems has no such rewrite (a plain proxy, real KRA's resultCd is trusted).
"""

from decimal import Decimal

from pydantic import BaseModel, Field

from app.integrations.vscu.schemas.envelope import VSCURequestBase, VSCUResponseEnvelope


class StockMoveReq(VSCURequestBase):
    lastReqDt: str


class StockMoveItem(BaseModel):
    itemSeq: int
    itemCd: str
    itemClsCd: str | None = None
    itemNm: str | None = None
    bcd: str | None = None
    pkgUnitCd: str | None = None
    pkg: Decimal | None = None
    qtyUnitCd: str | None = None
    qty: Decimal | None = None
    itemExprDt: str | None = None
    prc: Decimal | None = None
    splyAmt: Decimal | None = None
    totDcAmt: Decimal | None = None
    taxblAmt: Decimal | None = None
    taxTyCd: str | None = None
    taxAmt: Decimal | None = None
    totAmt: Decimal | None = None


class StockMove(BaseModel):
    custTin: str | None = None
    custBhfId: str | None = None
    sarNo: int | None = None
    ocrnDt: str | None = None
    totItemCnt: int | None = None
    totTaxblAmt: Decimal | None = None
    totTaxAmt: Decimal | None = None
    totAmt: Decimal | None = None
    remark: str | None = None
    itemList: list[StockMoveItem] = []


class StockMoveResData(BaseModel):
    stockList: list[StockMove] = []


class StockMoveRes(VSCUResponseEnvelope[StockMoveResData]):
    pass


class StockIoItemSaveReq(BaseModel):
    itemSeq: int
    itemCd: str
    itemClsCd: str
    itemNm: str
    bcd: str | None = None
    pkgUnitCd: str
    pkg: Decimal = Field(max_digits=13, decimal_places=2)
    qtyUnitCd: str
    qty: Decimal = Field(max_digits=13, decimal_places=2)
    itemExprDt: str | None = None
    prc: Decimal = Field(max_digits=18, decimal_places=2)
    splyAmt: Decimal = Field(max_digits=18, decimal_places=2)
    totDcAmt: Decimal = Field(max_digits=18, decimal_places=2)
    taxblAmt: Decimal = Field(max_digits=18, decimal_places=2)
    taxTyCd: str
    taxAmt: Decimal = Field(max_digits=18, decimal_places=2)
    totAmt: Decimal = Field(max_digits=18, decimal_places=2)


class StockIoSaveReq(VSCURequestBase):
    sarNo: int
    orgSarNo: int | None = None
    regTyCd: str = "M"
    custTin: str | None = None
    custNm: str | None = None
    custBhfId: str | None = None
    # Stock In/Out (cdCls 12): 01-06 incoming, 11-16 outgoing — raw KRA code, no crosswalk
    # given (matches the OSCU side's same documented gap, app/schemas/client/stock_in.py).
    sarTyCd: str
    ocrnDt: str
    totItemCnt: int
    totTaxblAmt: Decimal = Field(max_digits=18, decimal_places=2)
    totTaxAmt: Decimal = Field(max_digits=18, decimal_places=2)
    totAmt: Decimal = Field(max_digits=18, decimal_places=2)
    remark: str | None = None
    regrId: str
    regrNm: str
    modrId: str
    modrNm: str
    itemList: list[StockIoItemSaveReq]


class StockIoSaveRes(VSCUResponseEnvelope[dict[str, object]]):
    pass


class StockMasterSaveReq(VSCURequestBase):
    itemCd: str
    rsdQty: Decimal = Field(max_digits=18, decimal_places=2)
    regrId: str
    regrNm: str
    modrId: str
    modrNm: str


class StockMasterSaveRes(VSCUResponseEnvelope[dict[str, object]]):
    pass
