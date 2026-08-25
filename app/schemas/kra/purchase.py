"""Pydantic mirrors of purchase-sales search/save request/response (spec §6.7).

The source spec describes this endpoint family in prose rather than the full field table it
gives sales (§6.6), explicitly saying purchase "mirrors the sales structure" — so item lines
reuse TrnsSalesSaveWrItem's shape, and header amount/tax-bucket fields reuse sales' field
names and precisions. The wrapper key for /selectTrnsPurchaseSalesList's list of entries isn't
named in the source spec (unlike e.g. bhfList/custList/noticeList elsewhere) — "saleList" here
is a placeholder pending confirmation against a live sandbox response.
"""

from decimal import Decimal

from pydantic import BaseModel, Field

from app.schemas.kra.common import KraAuthenticatedRequest, KraResponseEnvelope
from app.schemas.kra.sales import TrnsSalesSaveWrItem


class PurchaseSalesEntry(BaseModel):
    spplrTin: str = Field(max_length=11)
    spplrBhfId: str = Field(max_length=2)
    spplrInvcNo: str
    rcptTyCd: str = Field(max_length=5)
    pmtTyCd: str = Field(max_length=5)
    cfmDt: str = Field(max_length=14)
    salesDt: str = Field(max_length=8)
    stockRlsDt: str | None = None
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

    itemList: list[TrnsSalesSaveWrItem]


class TrnsPurchaseSalesReq(KraAuthenticatedRequest):
    lastReqDt: str


class TrnsPurchaseSalesData(BaseModel):
    saleList: list[PurchaseSalesEntry]


class TrnsPurchaseSalesRes(KraResponseEnvelope[TrnsPurchaseSalesData]):
    pass


class PurchaseItem(TrnsSalesSaveWrItem):
    """StockIoItemSaveReq-style purchase line: sales item fields plus itemExprDt (spec §6.7)."""

    itemExprDt: str | None = None


class TrnsPurchaseSaveReq(KraAuthenticatedRequest):
    """POST /insertTrnsPurchase. regTyCd (Automatic/Manual, §12.13), pchsTyCd (Transaction
    Type, §12.9), rcptTyCd (P=Purchase / R=Credit Note after Purchase), pchsSttsCd
    (Transaction Progress, §12.11) — see spec §6.7 and §12 for the underlying code lists.
    """

    invcNo: str = Field(max_length=38)
    orgInvcNo: str = Field(max_length=38)
    spplrTin: str = Field(max_length=11)
    spplrBhfId: str = Field(max_length=2)
    spplrInvcNo: str
    regTyCd: str = Field(max_length=5)
    pchsTyCd: str = Field(max_length=5)
    rcptTyCd: str = Field(max_length=5)
    pchsSttsCd: str = Field(max_length=5)
    pchsDt: str = Field(max_length=8)
    cfmDt: str = Field(max_length=14)
    salesDt: str = Field(max_length=8)
    stockRlsDt: str | None = None
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

    regrId: str = Field(max_length=20)
    regrNm: str = Field(max_length=60)
    modrId: str = Field(max_length=20)
    modrNm: str = Field(max_length=60)

    itemList: list[PurchaseItem]


class TrnsPurchaseSaveRes(KraResponseEnvelope[None]):
    pass
