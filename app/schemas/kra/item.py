"""Pydantic mirrors of item classification/save/search request/response (spec §6.3)."""

from decimal import Decimal

from pydantic import BaseModel, Field

from app.schemas.kra.common import KraAuthenticatedRequest, KraResponseEnvelope


class ItemClsSearchReq(KraAuthenticatedRequest):
    lastReqDt: str


class ItemCls(BaseModel):
    itemClsCd: str
    itemClsNm: str
    itemClsLvl: str
    taxTyCd: str
    mjrTgYn: str
    useYn: str


class ItemClsSearchData(BaseModel):
    itemClsList: list[ItemCls]


class ItemClsSearchRes(KraResponseEnvelope[ItemClsSearchData]):
    pass


class ItemSaveReq(KraAuthenticatedRequest):
    """POST /saveItem. Required/optional split and lengths are per spec §6.3 verbatim; optional
    fields with no length given in the source spec are left unconstrained.
    """

    itemClsCd: str = Field(max_length=10)
    itemCd: str = Field(max_length=20)
    itemTyCd: str = Field(max_length=5)
    itemNm: str = Field(max_length=200)
    orgnNatCd: str = Field(max_length=5)
    pkgUnitCd: str = Field(max_length=5)
    qtyUnitCd: str = Field(max_length=5)
    taxTyCd: str = Field(max_length=5)
    dftPrc: Decimal = Field(max_digits=18, decimal_places=2)
    isrcAplcbYn: str = Field(max_length=1)
    useYn: str = Field(max_length=1)
    regrId: str = Field(max_length=20)
    regrNm: str = Field(max_length=60)
    modrId: str = Field(max_length=20)
    modrNm: str = Field(max_length=60)

    itemStdNm: str | None = None
    btchNo: str | None = None
    bcd: str | None = None
    grpPrcL1: Decimal | None = None
    grpPrcL2: Decimal | None = None
    grpPrcL3: Decimal | None = None
    grpPrcL4: Decimal | None = None
    grpPrcL5: Decimal | None = None
    addInfo: str | None = None
    sftyQty: Decimal | None = None


class ItemSaveRes(KraResponseEnvelope[None]):
    pass


class ItemSearchReq(KraAuthenticatedRequest):
    lastReqDt: str


class ItemListEntry(BaseModel):
    """A registered item as returned by /selectItemList — ItemSaveReq's fields plus the
    KRA-assigned regBhfId and rraModYn (whether KRA modified our classification code).
    """

    itemClsCd: str
    itemCd: str
    itemTyCd: str
    itemNm: str
    orgnNatCd: str
    pkgUnitCd: str
    qtyUnitCd: str
    taxTyCd: str
    dftPrc: Decimal
    isrcAplcbYn: str
    useYn: str
    regrId: str
    regrNm: str
    modrId: str
    modrNm: str
    regBhfId: str
    rraModYn: str

    itemStdNm: str | None = None
    btchNo: str | None = None
    bcd: str | None = None
    grpPrcL1: Decimal | None = None
    grpPrcL2: Decimal | None = None
    grpPrcL3: Decimal | None = None
    grpPrcL4: Decimal | None = None
    grpPrcL5: Decimal | None = None
    addInfo: str | None = None
    sftyQty: Decimal | None = None


class ItemSearchData(BaseModel):
    itemList: list[ItemListEntry]


class ItemSearchRes(KraResponseEnvelope[ItemSearchData]):
    pass
