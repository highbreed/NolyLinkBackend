"""Pydantic mirror of /saveTrnsSalesOsdc request/response (spec §6.6) — the fiscal receipt path.

Tax-bucket fields (taxblAmt/taxRt/taxAmt suffixed A..E) map to KRA tax types A-E (spec §7:
A=Exempt, B=16%, C=0%, D=Non-VAT, E=8%). totTaxblAmt/totTaxAmt/totAmt aren't given an explicit
length in the source spec text; (18,2) is inferred from the A..E buckets they aggregate.
regrId/regrNm/modrId/modrNm lengths are inferred from the identical fields' lengths in
ItemSaveReq (spec §6.3), since the source spec doesn't restate them here.
"""

from decimal import Decimal

from pydantic import BaseModel, Field

from app.schemas.kra.common import KraAuthenticatedRequest, KraResponseEnvelope


class TrnsSalesSaveWrReceipt(BaseModel):
    rcptPbctDt: str = Field(max_length=14)
    prchrAcptcYn: str = Field(max_length=1)

    custTin: str | None = Field(default=None, max_length=11)
    custMblNo: str | None = None
    trdeNm: str | None = None
    adrs: str | None = None
    topMsg: str | None = None
    btmMsg: str | None = None


class TrnsSalesSaveWrItem(BaseModel):
    itemSeq: int
    itemCd: str = Field(max_length=20)
    itemNm: str = Field(max_length=200)
    pkgUnitCd: str = Field(max_length=5)
    pkg: Decimal = Field(max_digits=13, decimal_places=2)
    qtyUnitCd: str = Field(max_length=5)
    qty: Decimal = Field(max_digits=13, decimal_places=2)
    prc: Decimal = Field(max_digits=18, decimal_places=2)
    splyAmt: Decimal = Field(max_digits=18, decimal_places=2)
    dcRt: Decimal = Field(max_digits=5, decimal_places=2)
    dcAmt: Decimal = Field(max_digits=18, decimal_places=2)
    taxTyCd: str = Field(max_length=5)
    taxblAmt: Decimal = Field(max_digits=18, decimal_places=2)
    totTaxAmt: Decimal = Field(max_digits=18, decimal_places=2)
    totAmt: Decimal = Field(max_digits=18, decimal_places=2)

    itemClsCd: str | None = None
    bcd: str | None = None
    isrccCd: str | None = None
    isrccNm: str | None = None
    isrcRt: Decimal | None = None
    isrcAmt: Decimal | None = None


class TrnsSalesSaveWrReq(KraAuthenticatedRequest):
    trdInvcNo: str = Field(max_length=50)
    invcNo: str = Field(max_length=38)
    orgInvcNo: str = Field(max_length=38)
    rcptTyCd: str = Field(max_length=5)
    salesSttsCd: str = Field(max_length=5)
    cfmDt: str = Field(max_length=14)
    salesDt: str = Field(max_length=8)
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

    prchrAcptcYn: str = Field(max_length=1)
    regrId: str = Field(max_length=20)
    regrNm: str = Field(max_length=60)
    modrId: str = Field(max_length=20)
    modrNm: str = Field(max_length=60)

    custTin: str | None = Field(default=None, max_length=11)
    custNm: str | None = Field(default=None, max_length=60)
    pmtTyCd: str | None = Field(default=None, max_length=5)
    stockRlsDt: str | None = None
    cnclReqDt: str | None = None
    cnclDt: str | None = None
    rfdDt: str | None = None
    rfdRsnCd: str | None = None
    remark: str | None = None

    receipt: TrnsSalesSaveWrReceipt
    itemList: list[TrnsSalesSaveWrItem]


class TrnsSalesSaveWrResData(BaseModel):
    """The fiscal receipt proof — must be persisted and returned to the originating client."""

    curRcptNo: int
    totRcptNo: int
    intrlData: str = Field(max_length=26)
    rcptSign: str = Field(max_length=16)
    sdcDateTime: str = Field(max_length=14)


class TrnsSalesSaveWrRes(KraResponseEnvelope[TrnsSalesSaveWrResData]):
    pass
