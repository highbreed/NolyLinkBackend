"""POST /trnsSales/* — verified against decompiled TrnsSalesSaveWrReq/Item/Receipt/Res,
InvoiceDetailsReq/Res/InvoiceItem/InvoiceReceipt, and TrnsPurchaseSalesReq (used verbatim as
the request type for BOTH /trnsSales/selectTrnsSalesList and /trnsPurchase/
selectTrnsPurchaseSales in the real controller signature — TrnsSalesExcute.selectTrnsSalesList
takes `TrnsPurchaseSalesReq`, not the differently-named-but-identically-shaped `InvoicesReq`
DTO that also exists unused in the jar) — all in com.kra.vsdc.execute.model,
vscu-v2.0.21.jar.

NOT the OSCU shape (app/schemas/kra/sales.py) — deliberately kept separate. Two confirmed
field-level differences that make silently reusing the OSCU schema unsafe:
  - Per-line tax amount is named `taxAmt` here, `totTaxAmt` on OSCU's TrnsSalesSaveWrItem.
  - `salesTyCd` is required here (OSCU has no equivalent) — verified via
    TrnsSalesExcute.saveTrnsSales: valid (salesTyCd, rcptTyCd) pairs are exactly
    NS/NR/TS/TR/CS/CR/PS (the controller's own 834 error message spells this out); anything
    else is rejected before ever reaching real KRA. "N" + "S" (Normal Sale) is the standard
    case for a plain new invoice.
"""

from decimal import Decimal

from pydantic import BaseModel, Field

from app.integrations.vscu.schemas.envelope import VSCURequestBase, VSCUResponseEnvelope


class TrnsSalesSaveWrReceipt(BaseModel):
    """Receipt sub-object. curRcptNo/totRcptNo/rcptPbctDt/intrlData/rcptSign are VSCU-owned
    output values (SequenceManager/InternalManager set them server-side) — do not fabricate
    them; leave unset on a request and read the confirmed values back from the response.
    """

    curRcptNo: int | None = None
    totRcptNo: int | None = None
    custTin: str | None = None
    custMblNo: str | None = None
    rptNo: int | None = None
    rcptPbctDt: str | None = None
    intrlData: str | None = None
    rcptSign: str | None = None
    jrnl: str | None = None
    trdeNm: str | None = None
    adrs: str | None = None
    topMsg: str | None = None
    btmMsg: str | None = None
    prchrAcptcYn: str | None = None


class TrnsSalesSaveWrItem(BaseModel):
    itemSeq: int
    itemCd: str
    itemClsCd: str
    itemNm: str
    bcd: str | None = None
    pkgUnitCd: str
    pkg: Decimal = Field(max_digits=13, decimal_places=2)
    qtyUnitCd: str
    qty: Decimal = Field(max_digits=13, decimal_places=2)
    prc: Decimal = Field(max_digits=18, decimal_places=2)
    splyAmt: Decimal = Field(max_digits=18, decimal_places=2)
    dcRt: Decimal = Field(max_digits=5, decimal_places=2)
    dcAmt: Decimal = Field(max_digits=18, decimal_places=2)
    isrccCd: str | None = None
    isrccNm: str | None = None
    isrcRt: Decimal | None = None
    isrcAmt: Decimal | None = None
    taxTyCd: str
    taxblAmt: Decimal = Field(max_digits=18, decimal_places=2)
    taxAmt: Decimal = Field(max_digits=18, decimal_places=2)
    totAmt: Decimal = Field(max_digits=18, decimal_places=2)


class TrnsSalesSaveWrReq(VSCURequestBase):
    invcNo: int
    orgInvcNo: int = 0

    custTin: str | None = None
    custNm: str | None = None

    # "N"=Normal, "T"=Training, "P"=Proforma, "C"=Copy — see module docstring.
    salesTyCd: str = "N"
    rcptTyCd: str = "S"
    # Optional, matching OSCU's own TrnsSalesSaveWrReq (app/schemas/kra/sales.py) — a credit
    # note (rcptTyCd="R") doesn't necessarily have a payment method of its own, and nothing in
    # the decompiled DTO (no Bean Validation annotations present) forces this required either.
    pmtTyCd: str | None = None
    salesSttsCd: str

    cfmDt: str
    salesDt: str
    stockRlsDt: str | None = None

    cnclReqDt: str | None = None
    cnclDt: str | None = None
    rfdDt: str | None = None
    rfdRsnCd: str | None = None

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

    prchrAcptcYn: str | None = None
    remark: str | None = None

    regrId: str
    regrNm: str
    modrId: str
    modrNm: str

    receipt: TrnsSalesSaveWrReceipt
    itemList: list[TrnsSalesSaveWrItem]


class TrnsSalesSaveWrResData(BaseModel):
    """The fiscal receipt proof. Persist all of it — see app/db/models.py's SalesInvoice
    (reused as-is for VSCU; same fiscal-record shape OSCU already writes to)."""

    rcptNo: int | None = None
    intrlData: str | None = None
    rcptSign: str | None = None
    totRcptNo: int | None = None
    vsdcRcptPbctDate: str | None = None
    sdcId: str | None = None
    mrcNo: str | None = None


class TrnsSalesSaveWrRes(VSCUResponseEnvelope[TrnsSalesSaveWrResData]):
    pass


class InvoiceDetailsReq(VSCURequestBase):
    invcNo: str


class InvoiceReceipt(BaseModel):
    custTin: str | None = None
    custMblNo: str | None = None
    rcptPbctDt: str | None = None
    rptNo: int | None = None
    trdeNm: str | None = None
    adrs: str | None = None
    topMsg: str | None = None
    btmMsg: str | None = None
    prchrAcptcYn: str | None = None
    intrlData: str | None = None
    rcptSign: str | None = None
    curRcptNo: str | None = None
    totRcptNo: str | None = None
    sdcDateTime: str | None = None


class InvoiceItem(BaseModel):
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
    isrccCd: str | None = None
    isrccNm: str | None = None
    isrcRt: Decimal | None = None
    isrcAmt: Decimal | None = None
    taxTyCd: str | None = None
    taxblAmt: Decimal | None = None
    taxAmt: Decimal | None = None
    totAmt: Decimal | None = None


class InvoiceDetailsResData(BaseModel):
    invcNo: int
    orgInvcNo: int | None = None
    custTin: str | None = None
    custNm: str | None = None
    rcptTyCd: str | None = None
    pmtTyCd: str | None = None
    salesSttsCd: str | None = None
    cfmDt: str | None = None
    salesDt: str | None = None
    stockRlsDt: str | None = None
    cnclReqDt: str | None = None
    cnclDt: str | None = None
    rfdDt: str | None = None
    rfdRsnCd: str | None = None
    totItemCnt: int | None = None
    taxblAmtA: Decimal | None = None
    taxblAmtB: Decimal | None = None
    taxblAmtC: Decimal | None = None
    taxblAmtD: Decimal | None = None
    taxblAmtE: Decimal | None = None
    taxRtA: Decimal | None = None
    taxRtB: Decimal | None = None
    taxRtC: Decimal | None = None
    taxRtD: Decimal | None = None
    taxRtE: Decimal | None = None
    taxAmtA: Decimal | None = None
    taxAmtB: Decimal | None = None
    taxAmtC: Decimal | None = None
    taxAmtD: Decimal | None = None
    taxAmtE: Decimal | None = None
    totTaxblAmt: Decimal | None = None
    totTaxAmt: Decimal | None = None
    totAmt: Decimal | None = None
    prchrAcptcYn: str | None = None
    remark: str | None = None
    regrId: str | None = None
    regrNm: str | None = None
    modrId: str | None = None
    modrNm: str | None = None
    receipt: InvoiceReceipt | None = None
    itemList: list[InvoiceItem] = []


class InvoiceDataResList(BaseModel):
    salesList: list[InvoiceDetailsResData] = []


class InvoiceDetailsRes(VSCUResponseEnvelope[InvoiceDataResList]):
    pass


class TrnsSalesListReq(VSCURequestBase):
    """Request type for POST /trnsSales/selectTrnsSalesList — see module docstring: the real
    controller parameter type is (the identically-shaped) TrnsPurchaseSalesReq, not the
    differently-named InvoicesReq DTO that also exists in the jar but is unused by it.
    """

    lastReqDt: str


class InvoiceResList(BaseModel):
    salesList: list[InvoiceDetailsResData] = []


class InvoicesRes(VSCUResponseEnvelope[InvoiceResList]):
    pass
