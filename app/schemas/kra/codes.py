"""Pydantic mirrors of code/customer/notice search request/response (spec §6.2)."""

from pydantic import BaseModel, Field

from app.schemas.kra.common import KraAuthenticatedRequest, KraResponseEnvelope


class CodeSearchReq(KraAuthenticatedRequest):
    lastReqDt: str


class CodeDetail(BaseModel):
    cd: str
    cdNm: str
    cdDesc: str
    srtOrd: int
    userDfnCd1: str | None = None
    userDfnCd2: str | None = None
    userDfnCd3: str | None = None
    useYn: str


class CodeClass(BaseModel):
    cdCls: str
    cdClsNm: str
    cdClsDesc: str
    userDfnNm1: str | None = None
    userDfnNm2: str | None = None
    userDfnNm3: str | None = None
    useYn: str
    dtlList: list[CodeDetail]


class CodeSearchData(BaseModel):
    clsList: list[CodeClass]


class CodeSearchRes(KraResponseEnvelope[CodeSearchData]):
    pass


class CustSearchReq(KraAuthenticatedRequest):
    custmTin: str = Field(max_length=11)


class Customer(BaseModel):
    tin: str
    taxprNm: str
    taxprSttsCd: str
    prvncNm: str
    dstrtNm: str
    sctrNm: str
    locDesc: str


class CustSearchData(BaseModel):
    custList: list[Customer]


class CustSearchRes(KraResponseEnvelope[CustSearchData]):
    pass


class NoticeSearchReq(KraAuthenticatedRequest):
    lastReqDt: str


class Notice(BaseModel):
    noticeNo: str
    title: str
    cont: str
    dtlUrl: str
    regrNm: str
    regDt: str


class NoticeSearchData(BaseModel):
    noticeList: list[Notice]


class NoticeSearchRes(KraResponseEnvelope[NoticeSearchData]):
    pass
