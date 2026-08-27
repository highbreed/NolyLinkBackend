"""POST /v1/sales-invoices, GET /v1/sales-invoices/{external_ref} (spec §18).

Sales invoices go straight through to KRA synchronously (spec §14's own recommendation,
since the client typically needs rcptSign/curRcptNo immediately to print a compliant
receipt) — unlike purchases/stock/imports (Phase 8), which use the outbox pattern.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Client, SalesInvoice
from app.db.session import get_db
from app.schemas.client.sales_in import ClientSalesInvoice
from app.services import idempotency
from app.services.client_auth import get_authorized_branch
from app.services.kra_adapter import KraAdapter, KraError
from app.services.mapping.generic_mapper import CodeLookupError, map_client_sales_invoice_to_kra
from app.services.rate_limit import enforce_rate_limit

router = APIRouter()

ENDPOINT_NAME = "/v1/sales-invoices"


class SalesInvoiceOut(BaseModel):
    external_ref: str
    branch_code: str
    status: str
    kra_cur_rcpt_no: int | None = None
    kra_tot_rcpt_no: int | None = None
    kra_rcpt_sign: str | None = None
    kra_intrl_data: str | None = None
    kra_sdc_datetime: str | None = None


def _to_out(invoice: SalesInvoice, branch_code: str) -> SalesInvoiceOut:
    return SalesInvoiceOut(
        external_ref=invoice.external_ref,
        branch_code=branch_code,
        status=invoice.status,
        kra_cur_rcpt_no=invoice.kra_cur_rcpt_no,
        kra_tot_rcpt_no=invoice.kra_tot_rcpt_no,
        kra_rcpt_sign=invoice.kra_rcpt_sign,
        kra_intrl_data=invoice.kra_intrl_data,
        kra_sdc_datetime=invoice.kra_sdc_datetime,
    )


@router.post("", response_model=SalesInvoiceOut, status_code=201)
async def submit_sales_invoice(
    payload: ClientSalesInvoice,
    client: Client = Depends(enforce_rate_limit),
    db: AsyncSession = Depends(get_db),
) -> SalesInvoiceOut:
    branch = await get_authorized_branch(db, client, payload.branch_code)

    try:
        cached = await idempotency.claim_or_get_cached(
            db, client.id, payload.external_invoice_ref, ENDPOINT_NAME
        )
    except idempotency.IdempotencyInProgress as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if cached is not None:
        # Same external_ref already fiscalized — return the cached KRA response instead of
        # submitting again (spec §13; this is also how KRA code 994 "Overlapped data" is
        # naturally avoided rather than handled after the fact).
        await db.commit()
        return SalesInvoiceOut(
            external_ref=payload.external_invoice_ref,
            branch_code=branch.branch_code,
            status="FISCALIZED",
            kra_cur_rcpt_no=cached.get("curRcptNo"),
            kra_tot_rcpt_no=cached.get("totRcptNo"),
            kra_rcpt_sign=cached.get("rcptSign"),
            kra_intrl_data=cached.get("intrlData"),
            kra_sdc_datetime=cached.get("sdcDateTime"),
        )

    kra = KraAdapter(db=db)
    try:
        try:
            req = await map_client_sales_invoice_to_kra(
                db, branch, payload, regr_id="gateway", regr_nm="OSCU Gateway"
            )
        except CodeLookupError as exc:
            await idempotency.release_claim(
                db, client.id, payload.external_invoice_ref, ENDPOINT_NAME
            )
            await db.commit()
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        try:
            res = await kra.save_sales_transaction(req, branch_id=branch.id)
        except KraError as exc:
            await idempotency.release_claim(
                db, client.id, payload.external_invoice_ref, ENDPOINT_NAME
            )
            await db.commit()
            raise HTTPException(
                status_code=502, detail=f"KRA error {exc.result_cd}: {exc.result_msg}"
            ) from exc

        if res.data is None:
            await idempotency.release_claim(
                db, client.id, payload.external_invoice_ref, ENDPOINT_NAME
            )
            await db.commit()
            raise HTTPException(
                status_code=502, detail="KRA returned resultCd=000 with no receipt data"
            )

        invoice = SalesInvoice(
            branch_id=branch.id,
            external_ref=payload.external_invoice_ref,
            kra_invc_no=int(req.invcNo),
            kra_cur_rcpt_no=res.data.curRcptNo,
            kra_tot_rcpt_no=res.data.totRcptNo,
            kra_rcpt_sign=res.data.rcptSign,
            kra_intrl_data=res.data.intrlData,
            kra_sdc_datetime=res.data.sdcDateTime,
            status="FISCALIZED",
        )
        db.add(invoice)

        await idempotency.store_response(
            db,
            client.id,
            payload.external_invoice_ref,
            ENDPOINT_NAME,
            {
                "curRcptNo": res.data.curRcptNo,
                "totRcptNo": res.data.totRcptNo,
                "rcptSign": res.data.rcptSign,
                "intrlData": res.data.intrlData,
                "sdcDateTime": res.data.sdcDateTime,
            },
        )
        await db.commit()
    finally:
        await kra.aclose()

    return _to_out(invoice, branch.branch_code)


@router.get("/{external_ref}", response_model=SalesInvoiceOut)
async def get_sales_invoice(
    external_ref: str,
    branch_code: str,
    client: Client = Depends(enforce_rate_limit),
    db: AsyncSession = Depends(get_db),
) -> SalesInvoiceOut:
    branch = await get_authorized_branch(db, client, branch_code)

    invoice = await db.scalar(
        select(SalesInvoice).where(
            SalesInvoice.branch_id == branch.id, SalesInvoice.external_ref == external_ref
        )
    )
    if invoice is None:
        raise HTTPException(
            status_code=404, detail=f"No sales invoice found for external_ref={external_ref!r}"
        )
    return _to_out(invoice, branch.branch_code)
