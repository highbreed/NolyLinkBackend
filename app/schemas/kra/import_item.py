"""Pydantic mirrors of import item search/update request/response (spec §6.5).

No field lengths are given in the source spec for these customs-declaration fields (unlike
§6.3/§6.6), so they're left unconstrained rather than inventing limits.
"""

from decimal import Decimal

from pydantic import BaseModel

from app.schemas.kra.common import KraAuthenticatedRequest, KraResponseEnvelope


class ImportItemSearchReq(KraAuthenticatedRequest):
    lastReqDt: str


class ImportItemEntry(BaseModel):
    taskCd: str
    dclDe: str
    itemSeq: int
    dclNo: str
    hsCd: str
    itemNm: str
    orgnNatCd: str
    exptNatCd: str
    pkg: Decimal
    pkgUnitCd: str
    qty: Decimal
    qtyUnitCd: str
    totWt: Decimal
    netWt: Decimal
    spplrNm: str
    agntNm: str
    invcFcurAmt: Decimal
    invcFcurCd: str
    invcFcurExcrt: Decimal


class ImportItemSearchData(BaseModel):
    itemList: list[ImportItemEntry]


class ImportItemSearchRes(KraResponseEnvelope[ImportItemSearchData]):
    pass


class ImportItemUpdateReq(KraAuthenticatedRequest):
    """POST updateImportItem. imptItemsttsCd values per spec §12.16:
    1 Unsent / 2 Waiting / 3 Approved / 4 Cancelled.
    """

    taskCd: str
    dclDe: str
    itemSeq: int
    hsCd: str
    itemClCd: str
    itemCd: str
    imptItemsttsCd: str


class ImportItemUpdateRes(KraResponseEnvelope[None]):
    pass
