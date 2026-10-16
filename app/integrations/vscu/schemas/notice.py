"""POST /notices/selectNotices — verified against decompiled NoticeReq/NoticeRes/Notice
(com.kra.vsdc.execute.model). Plain proxy to real KRA, no local logic.
"""

from pydantic import BaseModel

from app.integrations.vscu.schemas.envelope import VSCURequestBase, VSCUResponseEnvelope


class NoticeReq(VSCURequestBase):
    lastReqDt: str


class Notice(BaseModel):
    noticeNo: int
    title: str | None = None
    cont: str | None = None
    dtlUrl: str | None = None
    regrNm: str | None = None
    regDt: str | None = None


class NoticeResData(BaseModel):
    noticeList: list[Notice] = []


class NoticeRes(VSCUResponseEnvelope[NoticeResData]):
    pass
