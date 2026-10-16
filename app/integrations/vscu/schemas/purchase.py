"""POST /trnsPurchase/* — verified against decompiled TrnsPurchaseSalesReq/Res,
TrnsPurchaseSaveReq/Item/Res (com.kra.vsdc.execute.model, vscu-v2.0.21.jar).
TrnsPurchaseExcute has no local business logic (a plain proxy on both endpoints) — real KRA's
resultCd is trusted as-is, unlike /items/saveItems or /stock/saveStockItems.

Confirmed, not assumed: TrnsPurchaseSales (the supplier-declared-sale search response) has
only 4 tax buckets — A-D, no E — while TrnsPurchaseSaveReq (what we send to confirm) requires
all 5, A-E. When building a save request from a cached search result
(app/services/vscu_purchase_service.py), taxblAmtE/taxRtE/taxAmtE must be defaulted (0.00),
not read off the cached entry.
"""

from decimal import Decimal

from pydantic import BaseModel, Field

from app.integrations.vscu.schemas.envelope import VSCURequestBase, VSCUResponseEnvelope


class TrnsPurchaseSalesReq(VSCURequestBase):
    lastReqDt: str


class TrnsPurchaseSalesItem(BaseModel):
    itemSeq: int
    itemCd: str
    itemClsCd: str | None = None
    itemNm: str | None = None
    bcd: str | None = None
    pkgUnitCd: str | None = None
    pkg: Decimal | None = None
    qtyUnitCd: str | None = None
    qty: Decimal | None = None
    prc: Decimal | None = None
    splyAmt: Decimal | None = None
    dcRt: Decimal | None = None
    dcAmt: Decimal | None = None
    taxTyCd: str | None = None
    taxblAmt: Decimal | None = None
    taxAmt: Decimal | None = None
    totAmt: Decimal | None = None


class TrnsPurchaseSales(BaseModel):
    spplrTin: str
    spplrNm: str | None = None
    spplrBhfId: str
    spplrInvcNo: int
    rcptTyCd: str | None = None
    pmtTyCd: str | None = None
    cfmDt: str | None = None
    salesDt: str | None = None
    stockRlsDt: str | None = None
    totItemCnt: int | None = None

    # Confirmed only 4 buckets on this response — see module docstring.
    taxblAmtA: Decimal | None = None
    taxblAmtB: Decimal | None = None
    taxblAmtC: Decimal | None = None
    taxblAmtD: Decimal | None = None
    taxRtA: Decimal | None = None
    taxRtB: Decimal | None = None
    taxRtC: Decimal | None = None
    taxRtD: Decimal | None = None
    taxAmtA: Decimal | None = None
    taxAmtB: Decimal | None = None
    taxAmtC: Decimal | None = None
    taxAmtD: Decimal | None = None

    totTaxblAmt: Decimal | None = None
    totTaxAmt: Decimal | None = None
    totAmt: Decimal | None = None
    remark: str | None = None
    itemList: list[TrnsPurchaseSalesItem] = []


class TrnsPurchaseSalesResData(BaseModel):
    saleList: list[TrnsPurchaseSales] = []


class TrnsPurchaseSalesRes(VSCUResponseEnvelope[TrnsPurchaseSalesResData]):
    pass


class TrnsPurchaseSaveItem(BaseModel):
    itemSeq: int
    itemCd: str
    itemClsCd: str
    itemNm: str
    bcd: str | None = None
    spplrItemClsCd: str | None = None
    spplrItemCd: str | None = None
    spplrItemNm: str | None = None
    pkgUnitCd: str
    pkg: Decimal = Field(max_digits=13, decimal_places=2)
    qtyUnitCd: str
    qty: Decimal = Field(max_digits=13, decimal_places=2)
    prc: Decimal = Field(max_digits=18, decimal_places=2)
    splyAmt: Decimal = Field(max_digits=18, decimal_places=2)
    dcRt: Decimal = Field(max_digits=5, decimal_places=2)
    dcAmt: Decimal = Field(max_digits=18, decimal_places=2)
    taxblAmt: Decimal = Field(max_digits=18, decimal_places=2)
    taxTyCd: str
    taxAmt: Decimal = Field(max_digits=18, decimal_places=2)
    totAmt: Decimal = Field(max_digits=18, decimal_places=2)
    itemExprDt: str | None = None


class TrnsPurchaseSaveReq(VSCURequestBase):
    invcNo: int
    orgInvcNo: int = 0
    spplrTin: str
    spplrBhfId: str
    spplrNm: str | None = None
    spplrInvcNo: int
    regTyCd: str = "M"
    pchsTyCd: str
    rcptTyCd: str = "P"
    pmtTyCd: str | None = None
    pchsSttsCd: str
    cfmDt: str
    pchsDt: str
    wrhsDt: str | None = None
    cnclReqDt: str | None = None
    cnclDt: str | None = None
    rfdDt: str | None = None
    totItemCnt: int

    taxblAmtA: Decimal = Field(max_digits=18, decimal_places=2)
    taxblAmtB: Decimal = Field(max_digits=18, decimal_places=2)
    taxblAmtC: Decimal = Field(max_digits=18, decimal_places=2)
    taxblAmtD: Decimal = Field(max_digits=18, decimal_places=2)
    taxblAmtE: Decimal = Field(max_digits=18, decimal_places=2)

    taxRtA: Decimal = Field(max_digits=7, decimal_places=2)
    taxRtB: Decimal = Field(max_digits=7, decimal_places=2)
    taxRtC: Decimal = Field(max_digits=7, decimal_places=2)
    taxRtD: Decimal = Field(max_digits=7, decimal_places=2)
    taxRtE: Decimal = Field(max_digits=7, decimal_places=2)

    taxAmtA: Decimal = Field(max_digits=18, decimal_places=2)
    taxAmtB: Decimal = Field(max_digits=18, decimal_places=2)
    taxAmtC: Decimal = Field(max_digits=18, decimal_places=2)
    taxAmtD: Decimal = Field(max_digits=18, decimal_places=2)
    taxAmtE: Decimal = Field(max_digits=18, decimal_places=2)

    totTaxblAmt: Decimal = Field(max_digits=18, decimal_places=2)
    totTaxAmt: Decimal = Field(max_digits=18, decimal_places=2)
    totAmt: Decimal = Field(max_digits=18, decimal_places=2)

    remark: str | None = None
    regrId: str
    regrNm: str
    modrId: str
    modrNm: str

    itemList: list[TrnsPurchaseSaveItem]


class TrnsPurchaseSaveRes(VSCUResponseEnvelope[dict[str, object]]):
    pass
