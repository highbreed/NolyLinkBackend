"""POST /reports/saveZReports, POST /reports/checkZReport — verified against decompiled
ReportZSaveUpdateReq/ReportZSaveRes, ReportZCheckReq/Res/ReportZCheckData
(com.kra.vsdc.execute.model, vscu-v2.0.21.jar).

Confirmed, not assumed (per master-spec caution): the ACTIVE local controller's own request
DTO for /saveZReports is the small {tin, bhfId, rptDe} shape (ReportZSaveUpdateReq) — the
local VSCU app builds the actual Z-report body itself server-side
(DataZreportManager.createZReport) from its own locally-tracked receipt history and sends
that upstream; we never construct or see the full report payload. Sending a large internal
ReportZSaveReq-shaped structure to this endpoint would be wrong.
"""

from decimal import Decimal

from pydantic import BaseModel

from app.integrations.vscu.schemas.envelope import VSCURequestBase, VSCUResponseEnvelope


class ReportZSaveReq(VSCURequestBase):
    rptDe: str


class ReportZSaveRes(VSCUResponseEnvelope[dict[str, object]]):
    pass


class ReportZCheckReq(VSCURequestBase):
    rptDe: str


class ReportZCheckData(BaseModel):
    rcptPbctCnt: int | None = None
    rcptOpnNo: int | None = None
    rcptClsNo: int | None = None

    nrmRcptPbctCnt: int | None = None
    nrmRcptOpnNo: int | None = None
    nrmRcptClsNo: int | None = None
    nrmSalesAmt: Decimal | None = None
    nrmRfdAmt: Decimal | None = None
    nrmSalesTaxAmt: Decimal | None = None
    nrmRfdTaxAmt: Decimal | None = None

    cpyRcptPbctCnt: int | None = None
    cpyRcptOpnNo: int | None = None
    cpyRcptClsNo: int | None = None
    cpySalesAmt: Decimal | None = None
    cpyRfdAmt: Decimal | None = None
    cpySalesTaxAmt: Decimal | None = None
    cpyRfdTaxAmt: Decimal | None = None

    trnRcptPbctCnt: int | None = None
    trnRcptOpnNo: int | None = None
    trnRcptClsNo: int | None = None
    trnSalesAmt: Decimal | None = None
    trnRfdAmt: Decimal | None = None
    trnSalesTaxAmt: Decimal | None = None
    trnRfdTaxAmt: Decimal | None = None

    pfmRcptPbctCnt: int | None = None
    pfmRcptOpnNo: int | None = None
    pfmRcptClsNo: int | None = None
    pfmSalesAmt: Decimal | None = None
    pfmRfdAmt: Decimal | None = None
    pfmSalesTaxAmt: Decimal | None = None
    pfmRfdTaxAmt: Decimal | None = None


class ReportZCheckResData(BaseModel):
    checkCode: str | None = None
    zRpt: ReportZCheckData | None = None
    rcptRpt: ReportZCheckData | None = None


class ReportZCheckRes(VSCUResponseEnvelope[ReportZCheckResData]):
    pass
