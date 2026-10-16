"""/test/echoTest and /main/selectServerTime — verified against decompiled EchoTestReq/
EchoTestRes and MainExcute.selectServerTime (com.kra.vsdc.excute / .execute.model,
vscu-v2.0.21.jar).

selectServerTime returns a bare String (MainExcute.java: `public String
selectServerTime()`), not a JSON envelope — confirmed by reading the method signature
directly, not assumed. Modeled here as `str`, matching master-spec guidance not to assume a
JSON object until the real runtime response format was confirmed.
"""

from pydantic import BaseModel

from app.integrations.vscu.schemas.envelope import VSCURequestBase, VSCUResponseEnvelope


class EchoTestReq(VSCURequestBase):
    testStr: str


class EchoTestResData(BaseModel):
    testStr: str | None = None


class EchoTestRes(VSCUResponseEnvelope[EchoTestResData]):
    pass


# GET /main/selectServerTime has no request/response model — see client.get_server_time(),
# which returns the raw str.
