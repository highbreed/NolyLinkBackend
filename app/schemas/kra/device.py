"""Pydantic mirror of /selectInitOsdcInfo request/response (spec §6.1)."""

from pydantic import BaseModel, Field

from app.schemas.kra.common import KraResponseEnvelope


class DeviceVerificationReq(BaseModel):
    """Device/branch init request. Uses dvcSrlNo instead of cmcKey, which doesn't exist yet."""

    tin: str = Field(max_length=11)
    bhfId: str = Field(max_length=2)
    dvcSrlNo: str = Field(max_length=100)


class InitTaxpayer(BaseModel):
    tin: str
    taxprNm: str
    bsnsActv: str


class InitBranch(BaseModel):
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


class InitDevice(BaseModel):
    dvcId: str
    sdcId: str
    mrcNo: str
    cmcKey: str


class DeviceVerificationData(BaseModel):
    """Nesting keys (taxpayer/branch/device) aren't spelled out verbatim in the source spec
    text (it just lists the three sub-objects) — confirm against a live sandbox response
    before relying on these key names.
    """

    taxpayer: InitTaxpayer
    branch: InitBranch
    device: InitDevice


class DeviceVerificationRes(KraResponseEnvelope[DeviceVerificationData]):
    pass
