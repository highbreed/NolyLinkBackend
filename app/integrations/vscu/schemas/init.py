"""/initializer/* — verified against decompiled InitInfoReq/InitInfoRes/InitInfoVO
(com.kra.vsdc.execute.model, vscu-v2.0.21.jar) and InitializeVsdcExcute's controller logic.

InitInfoVO on the wire (as KRA's own upstream response nests it) includes intrlKey/signKey/
cmcKey — but InitializeVsdcExcute.selectInitInfo always nulls those three out of its own
response before returning to us (`res.getData().getInfo().setCmcKey(null)` etc., right after
persisting them into its own local key store). They're modeled here as always-None from our
point of view, not omitted, so a response that unexpectedly *does* carry one fails loudly via
validation rather than silently being ignored — see VSCUInitializationService.
"""

from pydantic import BaseModel

from app.integrations.vscu.schemas.envelope import VSCURequestBase, VSCUResponseEnvelope


class InitInfoReq(VSCURequestBase):
    dvcSrlNo: str


class InitInfoVO(BaseModel):
    """tin/bhfId stay required — they echo the request's own identity fields, so a null value
    there would mean something is deeply wrong, worth failing loudly on. Every other field
    below is taxpayer-profile-descriptive, never read back by VSCUInitializationService (see
    that module) — made nullable 2026-09-02 after a real device's response returned
    `locDesc: null` on an otherwise-successful (resultCd=000) call and this schema rejected the
    whole response over one unused descriptive field. No evidence any of them are ever
    guaranteed non-null in practice (the decompiled jar's Java `String` type doesn't
    distinguish nullable from non-nullable), so treat them all the same way rather than
    special-casing only the one field that happened to be observed null so far.
    """

    tin: str
    bhfId: str
    taxprNm: str | None = None
    bsnsActv: str | None = None
    bhfNm: str | None = None
    bhfOpenDt: str | None = None
    prvncNm: str | None = None
    dstrtNm: str | None = None
    sctrNm: str | None = None
    locDesc: str | None = None
    hqYn: str | None = None
    mgrNm: str | None = None
    mgrTelNo: str | None = None
    mgrEmail: str | None = None
    sdcId: str | None = None
    mrcNo: str | None = None
    dvcId: str | None = None
    # Always None by the time we see it — see module docstring.
    intrlKey: str | None = None
    signKey: str | None = None
    cmcKey: str | None = None
    lastPchsInvcNo: int | None = None
    lastSaleRcptNo: int | None = None
    lastInvcNo: int | None = None
    lastSaleInvcNo: int | None = None
    lastTrainInvcNo: int | None = None
    lastProfrmInvcNo: int | None = None
    lastCopyInvcNo: int | None = None
    vatTyCd: str | None = None


class InitInfoResData(BaseModel):
    info: InitInfoVO | None = None


class InitInfoRes(VSCUResponseEnvelope[InitInfoResData]):
    pass
