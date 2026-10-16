"""POST /customers/selectCustomer — verified against decompiled CustReq/CustRes/Customer
(com.kra.vsdc.execute.model). Plain proxy to real KRA, no local logic — usable for B2B PIN
verification before invoice fiscalization. Never persisted/cached (spec guidance: don't
overwrite SAP customer master data automatically) — see VSCUReferenceDataService.lookup_customer.
"""

from pydantic import BaseModel

from app.integrations.vscu.schemas.envelope import VSCURequestBase, VSCUResponseEnvelope


class CustReq(VSCURequestBase):
    custmTin: str


class Customer(BaseModel):
    tin: str
    taxprNm: str | None = None
    taxprSttsCd: str | None = None
    prvncNm: str | None = None
    dstrtNm: str | None = None
    sctrNm: str | None = None
    locDesc: str | None = None


class CustResData(BaseModel):
    custList: list[Customer] = []


class CustRes(VSCUResponseEnvelope[CustResData]):
    pass
