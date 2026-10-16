"""Client-facing self-service monitoring — a Client's own OAuth2 token, not the admin gate.
Everything here is scoped to Client.customer.allowed_branch_ids (spec §15's allow-listing,
enforced the same way as every other client-facing endpoint via get_authorized_branch) — a
customer can only ever see its own branches' activity, never another tenant's.

Three views: submitted invoices (own SalesInvoice rows), a call-log history (own
KraCallLog/VscuCallLog rows — never raw payloads, only what those tables already store), and
VSCU Z-reports (read-only "check", never "save" — triggering a fresh report build on the
device is an admin/diagnostic action, see app/api/v1/endpoints/vscu_reports.py).

OSCU Z-reporting is deliberately not exposed here: whether OSCU's protocol has a day-closing
equivalent has not been verified against the spec/decompiled jar the way VSCU's was (see
docs/kra_specs/README.md's verification policy) — GET .../z-report on an OSCU branch returns
400 rather than fabricating a contract that hasn't been confirmed to exist.
"""

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Client, KraCallLog, SalesInvoice, VscuCallLog
from app.db.session import get_db
from app.integrations.vscu.client import VSCUClient
from app.integrations.vscu.exceptions import VSCUError
from app.integrations.vscu.schemas.report import ReportZCheckReq
from app.services.client_auth import get_authorized_branch
from app.services.rate_limit import enforce_rate_limit

router = APIRouter()


class InvoiceSummary(BaseModel):
    external_ref: str
    branch_code: str
    transaction_kind: str
    original_ref: str | None
    status: str
    kra_cur_rcpt_no: int | None
    kra_rcpt_sign: str | None
    created_at: datetime


@router.get("/invoices", response_model=list[InvoiceSummary])
async def list_invoices(
    branch_code: str,
    transaction_kind: Literal["SALE", "CREDIT_NOTE"] | None = None,
    status: str | None = None,
    limit: int = Query(default=50, le=200, ge=1),
    client: Client = Depends(enforce_rate_limit),
    db: AsyncSession = Depends(get_db),
) -> list[InvoiceSummary]:
    branch = await get_authorized_branch(db, client, branch_code)

    stmt = (
        select(SalesInvoice)
        .where(SalesInvoice.branch_id == branch.id)
        .order_by(SalesInvoice.created_at.desc())
        .limit(limit)
    )
    if transaction_kind is not None:
        stmt = stmt.where(SalesInvoice.transaction_kind == transaction_kind)
    if status is not None:
        stmt = stmt.where(SalesInvoice.status == status)

    invoices = (await db.execute(stmt)).scalars().all()
    return [
        InvoiceSummary(
            external_ref=inv.external_ref,
            branch_code=branch.branch_code,
            transaction_kind=inv.transaction_kind,
            original_ref=inv.original_ref,
            status=inv.status,
            kra_cur_rcpt_no=inv.kra_cur_rcpt_no,
            kra_rcpt_sign=inv.kra_rcpt_sign,
            created_at=inv.created_at,
        )
        for inv in invoices
    ]


class CallLogEntry(BaseModel):
    protocol: Literal["OSCU", "VSCU"]
    endpoint: str
    result_cd: str | None
    result_msg: str | None
    result_dt: str | None
    http_status: int | None = None
    latency_ms: int | None = None
    created_at: datetime


@router.get("/call-logs", response_model=list[CallLogEntry])
async def list_call_logs(
    branch_code: str,
    result_cd: str | None = None,
    limit: int = Query(default=50, le=200, ge=1),
    client: Client = Depends(enforce_rate_limit),
    db: AsyncSession = Depends(get_db),
) -> list[CallLogEntry]:
    branch = await get_authorized_branch(db, client, branch_code)

    entries: list[CallLogEntry] = []
    if branch.device_type == "VSCU":
        vscu_stmt = (
            select(VscuCallLog)
            .where(VscuCallLog.branch_id == branch.id)
            .order_by(VscuCallLog.created_at.desc())
            .limit(limit)
        )
        if result_cd is not None:
            vscu_stmt = vscu_stmt.where(VscuCallLog.result_cd == result_cd)
        for vscu_row in (await db.execute(vscu_stmt)).scalars().all():
            entries.append(
                CallLogEntry(
                    protocol="VSCU",
                    endpoint=vscu_row.endpoint,
                    result_cd=vscu_row.result_cd,
                    result_msg=vscu_row.result_msg,
                    result_dt=vscu_row.result_dt,
                    http_status=vscu_row.http_status,
                    latency_ms=vscu_row.latency_ms,
                    created_at=vscu_row.created_at,
                )
            )
    else:
        kra_stmt = (
            select(KraCallLog)
            .where(KraCallLog.branch_id == branch.id)
            .order_by(KraCallLog.created_at.desc())
            .limit(limit)
        )
        if result_cd is not None:
            kra_stmt = kra_stmt.where(KraCallLog.result_cd == result_cd)
        for kra_row in (await db.execute(kra_stmt)).scalars().all():
            entries.append(
                CallLogEntry(
                    protocol="OSCU",
                    endpoint=kra_row.endpoint,
                    result_cd=kra_row.result_cd,
                    result_msg=kra_row.result_msg,
                    result_dt=kra_row.result_dt,
                    created_at=kra_row.created_at,
                )
            )

    return entries[:limit]


@router.get("/branches/{branch_code}/z-report/{rpt_de}")
async def get_z_report(
    branch_code: str,
    rpt_de: str,
    client: Client = Depends(enforce_rate_limit),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """rpt_de: the report date, YYYYMMDD (spec's rptDe). Read-only — see module docstring on
    why there's no client-facing "save" trigger, and why OSCU branches 400 here.
    """
    branch = await get_authorized_branch(db, client, branch_code)
    if branch.device_type != "VSCU":
        raise HTTPException(
            status_code=400,
            detail=(
                "Z-report retrieval is only available for VSCU branches today — OSCU's "
                "day-closing equivalent has not yet been verified against the spec."
            ),
        )

    vscu_client = VSCUClient(db=db)
    try:
        req = ReportZCheckReq(tin=branch.tin, bhfId=branch.bhf_id, rptDe=rpt_de)
        try:
            res = await vscu_client.check_z_report(branch.id, req)
        except VSCUError as exc:
            # str(exc), not exc.context.result_cd/result_msg: VSCUConfigurationError/
            # VSCUTransportError never carry those (no request attempted, or none ever got a
            # VSCU envelope back) — this produced "VSCU error None: None".
            raise HTTPException(status_code=502, detail=f"VSCU error: {exc}") from exc
        await db.commit()
    finally:
        await vscu_client.aclose()
    return res.model_dump(mode="json")
