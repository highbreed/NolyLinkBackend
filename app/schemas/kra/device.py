"""Pydantic mirror of /selectInitOsdcInfo request/response (spec §6.1).

Verified 2026-08-31 against KRA's own published "OSCU Requirements & Communication
Protocols v2.0" (April 2023, kra.go.ke/images/publications/OSCU_Specification_Document_v2.0.pdf)
§3.3.1 — the JSON sample there confirms the response nests taxpayer/branch/device fields
together under a single flat `data.info` object, not three separate sub-objects as originally
guessed (that guess was explicitly flagged as unconfirmed in this file's own prior revision).
"""

from pydantic import BaseModel, Field

from app.schemas.kra.common import KraResponseEnvelope


class DeviceVerificationReq(BaseModel):
    """Device/branch init request. Uses dvcSrlNo instead of cmcKey, which doesn't exist yet."""

    tin: str = Field(max_length=11)
    bhfId: str = Field(max_length=2)
    dvcSrlNo: str = Field(max_length=100)


class InitInfo(BaseModel):
    """Taxpayer, branch, and device fields all flattened into one object per the confirmed
    real response shape — spec §3.3.1's JSON sample nests everything under `data.info`.
    """

    tin: str
    taxprNm: str
    bsnsActv: str
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
    dvcId: str
    sdcId: str
    mrcNo: str
    cmcKey: str


class DeviceVerificationData(BaseModel):
    info: InitInfo


class DeviceVerificationRes(KraResponseEnvelope[DeviceVerificationData]):
    pass
