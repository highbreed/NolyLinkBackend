"""Pydantic mirrors of branch/customer/user/insurance request/response (spec §6.4)."""

from decimal import Decimal

from pydantic import BaseModel, Field

from app.schemas.kra.common import KraAuthenticatedRequest, KraResponseEnvelope


class BhfSearchReq(KraAuthenticatedRequest):
    lastReqDt: str


class BranchEntry(BaseModel):
    """Full branch master record, per spec §6.4 ("address, manager contact, hqYn")."""

    bhfId: str
    bhfNm: str
    bhfOpenDt: str
    prvncNm: str
    dstrtNm: str
    sctrNm: str
    locDesc: str
    hqYn: str
    mgrNm: str
    mgrTelNo: str
    mgrEmail: str


class BhfSearchData(BaseModel):
    bhfList: list[BranchEntry]


class BhfSearchRes(KraResponseEnvelope[BhfSearchData]):
    pass


class BhfCustSaveReq(KraAuthenticatedRequest):
    custNo: str = Field(max_length=9)
    custTin: str = Field(max_length=11)
    custNm: str = Field(max_length=60)

    adrs: str | None = None
    telNo: str | None = None
    email: str | None = None
    faxNo: str | None = None
    remark: str | None = None


class BhfCustSaveRes(KraResponseEnvelope[None]):
    pass


class BhfUserSaveReq(KraAuthenticatedRequest):
    userId: str = Field(max_length=20)
    userNm: str = Field(max_length=60)
    pwd: str = Field(max_length=255)


class BhfUserSaveRes(KraResponseEnvelope[None]):
    pass


class BhfInsuranceSaveReq(KraAuthenticatedRequest):
    """Pharmacy taxpayers only. isrcRt's spec entry ("NUMBER,3") doesn't state a decimal
    scale, only a 3-digit precision — treated as an unscaled 3-digit number pending
    confirmation against a live sandbox response.
    """

    isrccCd: str = Field(max_length=10)
    isrccNm: str = Field(max_length=100)
    isrcRt: Decimal = Field(max_digits=3)


class BhfInsuranceSaveRes(KraResponseEnvelope[None]):
    pass
