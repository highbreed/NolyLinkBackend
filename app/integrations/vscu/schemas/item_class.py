"""POST /itemClass/selectItemsClass — verified against decompiled ItemClsReq/ItemClsRes/
ItemClassLVO (com.kra.vsdc.execute.model). Plain proxy to real KRA, no local logic.
"""

from pydantic import BaseModel

from app.integrations.vscu.schemas.envelope import VSCURequestBase, VSCUResponseEnvelope


class ItemClsReq(VSCURequestBase):
    lastReqDt: str


class ItemClassLVO(BaseModel):
    itemClsCd: str
    itemClsNm: str | None = None
    itemClsLvl: int | None = None
    taxTyCd: str | None = None
    mjrTgYn: str | None = None
    useYn: str | None = None


class ItemClsResData(BaseModel):
    itemClsList: list[ItemClassLVO] = []


class ItemClsRes(VSCUResponseEnvelope[ItemClsResData]):
    pass
