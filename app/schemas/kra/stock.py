"""Pydantic mirrors of stock move/IO/master request/response (spec §6.8)."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.kra.common import KraAuthenticatedRequest, KraResponseEnvelope


class StockMoveReq(KraAuthenticatedRequest):
    lastReqDt: str


class StockMoveEntry(BaseModel):
    """The source spec gives no field list at all for /selectStockMoveList's response (just
    "inter-branch movement history") — permissive placeholder pending confirmation against a
    live sandbox response, rather than inventing field names.
    """

    model_config = ConfigDict(extra="allow")


class StockMoveData(BaseModel):
    stockMoveList: list[StockMoveEntry]


class StockMoveRes(KraResponseEnvelope[StockMoveData]):
    pass


class StockIoItemSaveReq(BaseModel):
    itemSeq: int
    itemClsCd: str
    itemNm: str
    pkgUnitCd: str
    pkg: Decimal
    qtyUnitCd: str
    qty: Decimal
    prc: Decimal
    splyAmt: Decimal
    totDcAmt: Decimal
    taxblAmt: Decimal
    taxTyCd: str
    taxAmt: Decimal
    totAmt: Decimal


class StockIOSaveReq(KraAuthenticatedRequest):
    """POST /insertStockIO. sarTyCd per spec §12.15 (01-06 incoming, 11-16 outgoing)."""

    sarNo: str
    orgSarNo: str
    regTyCd: str = Field(max_length=5)
    sarTyCd: str = Field(max_length=5)
    ocrnDt: str
    totItemCnt: int
    totTaxblAmt: Decimal = Field(max_digits=18, decimal_places=2)
    totTaxAmt: Decimal = Field(max_digits=18, decimal_places=2)
    totAmt: Decimal = Field(max_digits=18, decimal_places=2)
    regrId: str = Field(max_length=20)
    regrNm: str = Field(max_length=60)
    modrId: str = Field(max_length=20)
    modrNm: str = Field(max_length=60)
    itemList: list[StockIoItemSaveReq]


class StockIOSaveRes(KraResponseEnvelope[None]):
    pass


class StockMasterSaveReq(KraAuthenticatedRequest):
    itemCd: str = Field(max_length=20)
    rsdQty: Decimal


class StockMasterSaveRes(KraResponseEnvelope[None]):
    pass
