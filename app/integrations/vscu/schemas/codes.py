"""POST /code/selectCodes — verified against decompiled CodeReq/CodeRes/CodeClassLVO/
CodeDtlLVO (com.kra.vsdc.execute.model, vscu-v2.0.21.jar). CodeExcute.selectCodeList is a
plain proxy to real KRA (no local logic) — field shape matches OSCU's /selectCodeList
(app/schemas/kra/codes.py) exactly, which is why app/services/vscu_reference_data_service.py
reuses the same kra_code_lists cache table and "codes" sync-state resource key as OSCU: this
is the same KRA reference data, just fetched over a different transport.
"""

from pydantic import BaseModel

from app.integrations.vscu.schemas.envelope import VSCURequestBase, VSCUResponseEnvelope


class CodeReq(VSCURequestBase):
    lastReqDt: str


class CodeDtlLVO(BaseModel):
    cd: str
    cdNm: str | None = None
    cdDesc: str | None = None
    useYn: str | None = None
    srtOrd: int | None = None
    userDfnCd1: str | None = None
    userDfnCd2: str | None = None
    userDfnCd3: str | None = None


class CodeClassLVO(BaseModel):
    cdCls: str
    cdClsNm: str | None = None
    cdClsDesc: str | None = None
    useYn: str | None = None
    userDfnNm1: str | None = None
    userDfnNm2: str | None = None
    userDfnNm3: str | None = None
    dtlList: list[CodeDtlLVO] = []


class CodeResData(BaseModel):
    clsList: list[CodeClassLVO] = []


class CodeRes(VSCUResponseEnvelope[CodeResData]):
    pass
