"""POST /branches/* — verified against decompiled BhfReq/BhfRes/Bhf, BhfUserSaveReq/Res,
BhfInsuranceSaveReq/Res, BhfCustSaveReq/Res (com.kra.vsdc.execute.model). All four are plain
proxies to real KRA (BhfExcute has no local logic). The three save endpoints' responses all
declare `data` as `Object data = null` on the wire — no documented success payload shape, so
modeled as `dict[str, Any] | None` rather than guessing a structure (spec §40's rule against
inventing fields the source doesn't establish).

Note: BhfRes's response wraps `data.bhfList[]` — KRA's own jar names that nested class
`CodeResData` (a copy-paste artifact from CodeRes, confirmed in the decompiled source), not a
name to replicate here; only the JSON field names on the wire matter.
"""

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from app.integrations.vscu.schemas.envelope import VSCURequestBase, VSCUResponseEnvelope


class BhfReq(VSCURequestBase):
    lastReqDt: str


class Bhf(BaseModel):
    tin: str
    bhfId: str
    bhfNm: str | None = None
    bhfSttsCd: str | None = None
    prvncNm: str | None = None
    dstrtNm: str | None = None
    sctrNm: str | None = None
    locDesc: str | None = None
    mgrNm: str | None = None
    mgrTelNo: str | None = None
    mgrEmail: str | None = None
    hqYn: str | None = None


class BhfResData(BaseModel):
    bhfList: list[Bhf] = []


class BhfRes(VSCUResponseEnvelope[BhfResData]):
    pass


class BhfUserSaveReq(VSCURequestBase):
    userId: str
    userNm: str
    # Never logged — see app/integrations/vscu/client.py's request_hash-only logging, and
    # never included in any API response schema.
    pwd: str
    adrs: str | None = None
    cntc: str | None = None
    authCd: str | None = None
    remark: str | None = None
    useYn: str | None = None
    regrId: str
    regrNm: str
    modrId: str
    modrNm: str


class BhfUserSaveRes(VSCUResponseEnvelope[dict[str, Any]]):
    pass


class BhfInsuranceSaveReq(VSCURequestBase):
    isrccCd: str
    isrccNm: str
    isrcRt: Decimal = Field(max_digits=5, decimal_places=2)
    useYn: str | None = None
    regrId: str
    regrNm: str
    modrId: str
    modrNm: str


class BhfInsuranceSaveRes(VSCUResponseEnvelope[dict[str, Any]]):
    pass


class BhfCustSaveReq(VSCURequestBase):
    custNo: str
    custTin: str | None = None
    custNm: str
    adrs: str | None = None
    telNo: str | None = None
    email: str | None = None
    faxNo: str | None = None
    useYn: str | None = None
    remark: str | None = None
    regrId: str
    regrNm: str
    modrId: str
    modrNm: str


class BhfCustSaveRes(VSCUResponseEnvelope[dict[str, Any]]):
    pass
