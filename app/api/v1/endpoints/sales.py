"""POST /v1/sales-invoices, GET /v1/sales-invoices/{external_ref} (spec §18).

Sales invoices go straight through to KRA synchronously (spec §14's own recommendation,
since the client typically needs rcptSign/curRcptNo immediately to print a compliant
receipt) — unlike purchases/stock/imports (Phase 8), which use the outbox pattern.

This is the client-facing contract every ERP integration calls — one request shape
(ClientSalesInvoice) regardless of whether the branch is OSCU or VSCU underneath
(Branch.device_type). The OSCU path below is unchanged from before VSCU existed; the VSCU
path (app/services/vscu_sales_service.py) is a parallel implementation, not a refactor of the
OSCU one, since the two protocols' error/reconciliation semantics genuinely differ (see that
module's docstring). Shared: idempotency (app/services/idempotency.py) and the SalesInvoice
table itself — same fiscal-record shape, same idempotency keying, regardless of protocol.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Branch, Client, SalesInvoice
from app.db.session import get_db
from app.integrations.vscu.client import VSCUClient
from app.integrations.vscu.exceptions import VSCUError
from app.schemas.client.credit_note_in import ClientCreditNoteInvoice
from app.schemas.client.sales_in import ClientSalesInvoice
from app.services import idempotency
from app.services.client_auth import get_authorized_branch
from app.services.kra_adapter import KraAdapter, KraError
from app.services.kra_receipt import build_qr_link, dash_separated, format_scu_date
from app.services.mapping.generic_mapper import (
    CodeLookupError,
    map_client_credit_note_to_kra,
    map_client_sales_invoice_to_kra,
)
from app.services.rate_limit import enforce_rate_limit
from app.services.vscu_sales_service import VSCUSalesService, VSCUSequenceErrorNeedsResync

router = APIRouter()

ENDPOINT_NAME = "/v1/sales-invoices"
CREDIT_NOTE_ENDPOINT_NAME = "/v1/sales-invoices/credit-notes"


class ScuInformation(BaseModel):
    """Mirrors KRA's own "SCU Information" receipt block field-for-field (TIS-OSCU-VSCU-v2.0
    spec §6.23.1-6.23.7) — confirmed verbatim against a real receipt's KRA verification page
    (fetched 2026-10-16: https://etims-sbx.kra.go.ke/common/link/etims/receipt/
    indexEtimsReceiptData?Data=...). Print this block as shown rather than reformatting the
    top-level kra_* fields yourself — internal_data/receipt_signature here are already
    dash-grouped and control_unit_number/invoice_number/date already match the page's display
    format exactly.
    """

    date: str | None = None
    control_unit_number: str | None = None
    invoice_number: str | None = None
    internal_data: str | None = None
    receipt_signature: str | None = None
    receipt_number: int | None = None


class SalesInvoiceOut(BaseModel):
    external_ref: str
    branch_code: str
    status: str
    transaction_kind: str
    original_ref: str | None = None
    kra_cur_rcpt_no: int | None = None
    kra_tot_rcpt_no: int | None = None
    # Dash-grouped every 4 characters for display, matching KRA's own printed-receipt
    # convention (spec §6.23.6/§6.23.7) and its verification page — not the raw signature
    # used to build qrcode_link below (see app/services/kra_receipt.py's dash_separated).
    kra_rcpt_sign: str | None = None
    kra_intrl_data: str | None = None
    kra_sdc_datetime: str | None = None
    # The receipt's "CU ID" (TIS-OSCU-VSCU-v2.0 spec §6.23.3, e.g. "KRACU0300004030") and "CU
    # Invoice Number" (§6.23.4: CU ID + "/" + kra_cur_rcpt_no, e.g. "KRACU0300004030/1") — both
    # required on the printed receipt. See SalesInvoice.kra_sdc_id's docstring in
    # app/db/models.py for why OSCU and VSCU populate kra_sdc_id from different sources.
    kra_sdc_id: str | None = None
    kra_cu_invoice_no: str | None = None
    # KRA's receipt-verification QR link (TIS-OSCU-VSCU-v2.0 spec §6.23.8 /sample receipt
    # layout) — print this as a QR code on the receipt; see app/services/kra_receipt.py for
    # exactly how it's built and the spec ambiguity that function's docstring explains.
    qrcode_link: str | None = None
    # The same data as the kra_*/qrcode_link fields above, pre-formatted to match KRA's own
    # "SCU Information" receipt block exactly — see ScuInformation's own docstring.
    scu_information: ScuInformation | None = None


def _cached_str(cached: dict[str, object], key: str) -> str | None:
    """idempotency.claim_or_get_cached returns dict[str, object] (it's a generic JSON-blob
    store, not typed per-endpoint) — these narrow it back to what store_response actually put
    there for this endpoint, for mypy's benefit.
    """
    value = cached.get(key)
    return value if isinstance(value, str) else None


def _cached_int(cached: dict[str, object], key: str) -> int | None:
    value = cached.get(key)
    return value if isinstance(value, int) else None


def _build_cu_invoice_no(sdc_id: str | None, cur_rcpt_no: int | None) -> str | None:
    if not sdc_id or cur_rcpt_no is None:
        return None
    return f"{sdc_id}/{cur_rcpt_no}"


def _build_scu_information(
    sdc_datetime: str | None,
    sdc_id: str | None,
    cu_invoice_no: str | None,
    intrl_data: str | None,
    rcpt_sign: str | None,
    cur_rcpt_no: int | None,
) -> ScuInformation | None:
    if not rcpt_sign:
        return None
    return ScuInformation(
        date=format_scu_date(sdc_datetime),
        control_unit_number=sdc_id,
        invoice_number=cu_invoice_no,
        internal_data=dash_separated(intrl_data),
        receipt_signature=dash_separated(rcpt_sign),
        receipt_number=cur_rcpt_no,
    )


def _to_out(invoice: SalesInvoice, branch: Branch) -> SalesInvoiceOut:
    cu_invoice_no = _build_cu_invoice_no(invoice.kra_sdc_id, invoice.kra_cur_rcpt_no)
    return SalesInvoiceOut(
        external_ref=invoice.external_ref,
        branch_code=branch.branch_code,
        status=invoice.status,
        transaction_kind=invoice.transaction_kind,
        original_ref=invoice.original_ref,
        kra_cur_rcpt_no=invoice.kra_cur_rcpt_no,
        kra_tot_rcpt_no=invoice.kra_tot_rcpt_no,
        # Dash-grouped for display (KRA's own receipt convention, §6.23.6/§6.23.7 — see
        # dash_separated's docstring), not the raw value the QR link below needs.
        kra_rcpt_sign=dash_separated(invoice.kra_rcpt_sign),
        kra_intrl_data=dash_separated(invoice.kra_intrl_data),
        kra_sdc_datetime=invoice.kra_sdc_datetime,
        kra_sdc_id=invoice.kra_sdc_id,
        kra_cu_invoice_no=cu_invoice_no,
        qrcode_link=build_qr_link(branch.tin, branch.bhf_id, invoice.kra_rcpt_sign),
        scu_information=_build_scu_information(
            invoice.kra_sdc_datetime,
            invoice.kra_sdc_id,
            cu_invoice_no,
            invoice.kra_intrl_data,
            invoice.kra_rcpt_sign,
            invoice.kra_cur_rcpt_no,
        ),
    )


@router.post("", response_model=SalesInvoiceOut, status_code=201)
async def submit_sales_invoice(
    payload: ClientSalesInvoice,
    client: Client = Depends(enforce_rate_limit),
    db: AsyncSession = Depends(get_db),
) -> SalesInvoiceOut:
    branch = await get_authorized_branch(db, client, payload.branch_code)

    if branch.device_type == "VSCU":
        # VSCUSalesService.save_sale owns its *entire* idempotency lifecycle (claim, cached
        # check, release-on-error, store-on-success) internally — unlike the OSCU path below,
        # which claims once here and passes a "bare" request through KraAdapter. Do NOT also
        # claim here: claim_or_get_cached is not reentrant (a second claim attempt on a still-
        # unresolved key raises IdempotencyInProgress), so doing both would always collide.
        vscu_client = VSCUClient(db=db)
        try:
            try:
                invoice, _response = await VSCUSalesService(db, vscu_client).save_sale(
                    client.id, branch, payload
                )
            except idempotency.IdempotencyInProgress as exc:
                await db.rollback()
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except CodeLookupError as exc:
                await db.rollback()
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except VSCUSequenceErrorNeedsResync as exc:
                await db.rollback()
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except VSCUError as exc:
                await db.rollback()
                raise HTTPException(
                    status_code=502,
                    # str(exc), not exc.context.result_cd/result_msg: VSCUConfigurationError/
                    # VSCUTransportError never carry those (no request attempted, or none ever
                    # got a VSCU envelope back) — this produced "VSCU error None: None".
                    detail=f"VSCU error: {exc}",
                ) from exc
            except RuntimeError as exc:
                await db.rollback()
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            await db.commit()
        finally:
            await vscu_client.aclose()
        return _to_out(invoice, branch)

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
        cur_rcpt_no = _cached_int(cached, "curRcptNo")
        rcpt_sign = _cached_str(cached, "rcptSign")
        intrl_data = _cached_str(cached, "intrlData")
        sdc_datetime = _cached_str(cached, "sdcDateTime")
        cu_invoice_no = _build_cu_invoice_no(branch.sdc_id, cur_rcpt_no)
        return SalesInvoiceOut(
            external_ref=payload.external_invoice_ref,
            branch_code=branch.branch_code,
            status="FISCALIZED",
            transaction_kind="SALE",
            kra_cur_rcpt_no=cur_rcpt_no,
            kra_tot_rcpt_no=_cached_int(cached, "totRcptNo"),
            kra_rcpt_sign=dash_separated(rcpt_sign),
            kra_intrl_data=dash_separated(intrl_data),
            kra_sdc_datetime=sdc_datetime,
            kra_sdc_id=branch.sdc_id,
            kra_cu_invoice_no=cu_invoice_no,
            qrcode_link=build_qr_link(branch.tin, branch.bhf_id, rcpt_sign),
            scu_information=_build_scu_information(
                sdc_datetime,
                branch.sdc_id,
                cu_invoice_no,
                intrl_data,
                rcpt_sign,
                cur_rcpt_no,
            ),
        )

    kra = KraAdapter(db=db)
    try:
        try:
            req = await map_client_sales_invoice_to_kra(
                db,
                branch,
                payload,
                regr_id="gateway",
                regr_nm="OSCU Gateway",
                customer_id=client.customer_id,
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
            transaction_kind="SALE",
            kra_invc_no=int(req.invcNo),
            kra_cur_rcpt_no=res.data.curRcptNo,
            kra_tot_rcpt_no=res.data.totRcptNo,
            kra_rcpt_sign=res.data.rcptSign,
            kra_intrl_data=res.data.intrlData,
            kra_sdc_datetime=res.data.sdcDateTime,
            # OSCU's /saveTrnsSalesOsdc response carries no sdcId (unlike VSCU's) — the
            # branch's own value, set once at device init, is the only source here.
            kra_sdc_id=branch.sdc_id,
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

    return _to_out(invoice, branch)


@router.post("/credit-notes", response_model=SalesInvoiceOut, status_code=201)
async def submit_credit_note(
    payload: ClientCreditNoteInvoice,
    client: Client = Depends(enforce_rate_limit),
    db: AsyncSession = Depends(get_db),
) -> SalesInvoiceOut:
    """A credit note is its own fiscal transaction referencing the original sale via
    orgInvcNo (resolved from payload.original_invoice_ref against the SalesInvoice table) —
    see app/schemas/client/credit_note_in.py's module docstring for why, sourced from
    KRA_OSCU_FastAPI_Integration_Spec.md §7's code catalogue (canonical for both providers).
    Mirrors submit_sales_invoice's device_type dispatch and idempotency shape exactly.
    """
    branch = await get_authorized_branch(db, client, payload.branch_code)

    if branch.device_type == "VSCU":
        vscu_client = VSCUClient(db=db)
        try:
            try:
                invoice, _response = await VSCUSalesService(db, vscu_client).save_credit_note(
                    client.id, branch, payload
                )
            except idempotency.IdempotencyInProgress as exc:
                await db.rollback()
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except CodeLookupError as exc:
                await db.rollback()
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            except VSCUSequenceErrorNeedsResync as exc:
                await db.rollback()
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except VSCUError as exc:
                await db.rollback()
                raise HTTPException(
                    status_code=502,
                    # str(exc), not exc.context.result_cd/result_msg: VSCUConfigurationError/
                    # VSCUTransportError never carry those (no request attempted, or none ever
                    # got a VSCU envelope back) — this produced "VSCU error None: None".
                    detail=f"VSCU error: {exc}",
                ) from exc
            except RuntimeError as exc:
                await db.rollback()
                raise HTTPException(status_code=502, detail=str(exc)) from exc
            await db.commit()
        finally:
            await vscu_client.aclose()
        return _to_out(invoice, branch)

    try:
        cached = await idempotency.claim_or_get_cached(
            db, client.id, payload.external_invoice_ref, CREDIT_NOTE_ENDPOINT_NAME
        )
    except idempotency.IdempotencyInProgress as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if cached is not None:
        await db.commit()
        cur_rcpt_no = _cached_int(cached, "curRcptNo")
        rcpt_sign = _cached_str(cached, "rcptSign")
        intrl_data = _cached_str(cached, "intrlData")
        sdc_datetime = _cached_str(cached, "sdcDateTime")
        cu_invoice_no = _build_cu_invoice_no(branch.sdc_id, cur_rcpt_no)
        return SalesInvoiceOut(
            external_ref=payload.external_invoice_ref,
            branch_code=branch.branch_code,
            status="FISCALIZED",
            transaction_kind="CREDIT_NOTE",
            original_ref=payload.original_invoice_ref,
            kra_cur_rcpt_no=cur_rcpt_no,
            kra_tot_rcpt_no=_cached_int(cached, "totRcptNo"),
            kra_rcpt_sign=dash_separated(rcpt_sign),
            kra_intrl_data=dash_separated(intrl_data),
            kra_sdc_datetime=sdc_datetime,
            kra_sdc_id=branch.sdc_id,
            kra_cu_invoice_no=cu_invoice_no,
            qrcode_link=build_qr_link(branch.tin, branch.bhf_id, rcpt_sign),
            scu_information=_build_scu_information(
                sdc_datetime,
                branch.sdc_id,
                cu_invoice_no,
                intrl_data,
                rcpt_sign,
                cur_rcpt_no,
            ),
        )

    kra = KraAdapter(db=db)
    try:
        try:
            req = await map_client_credit_note_to_kra(
                db,
                branch,
                payload,
                regr_id="gateway",
                regr_nm="OSCU Gateway",
                customer_id=client.customer_id,
            )
        except CodeLookupError as exc:
            await idempotency.release_claim(
                db, client.id, payload.external_invoice_ref, CREDIT_NOTE_ENDPOINT_NAME
            )
            await db.commit()
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        try:
            res = await kra.save_sales_transaction(req, branch_id=branch.id)
        except KraError as exc:
            await idempotency.release_claim(
                db, client.id, payload.external_invoice_ref, CREDIT_NOTE_ENDPOINT_NAME
            )
            await db.commit()
            raise HTTPException(
                status_code=502, detail=f"KRA error {exc.result_cd}: {exc.result_msg}"
            ) from exc

        if res.data is None:
            await idempotency.release_claim(
                db, client.id, payload.external_invoice_ref, CREDIT_NOTE_ENDPOINT_NAME
            )
            await db.commit()
            raise HTTPException(
                status_code=502, detail="KRA returned resultCd=000 with no receipt data"
            )

        invoice = SalesInvoice(
            branch_id=branch.id,
            external_ref=payload.external_invoice_ref,
            transaction_kind="CREDIT_NOTE",
            original_ref=payload.original_invoice_ref,
            kra_invc_no=int(req.invcNo),
            kra_cur_rcpt_no=res.data.curRcptNo,
            kra_tot_rcpt_no=res.data.totRcptNo,
            kra_rcpt_sign=res.data.rcptSign,
            kra_intrl_data=res.data.intrlData,
            kra_sdc_datetime=res.data.sdcDateTime,
            kra_sdc_id=branch.sdc_id,
            status="FISCALIZED",
        )
        db.add(invoice)

        await idempotency.store_response(
            db,
            client.id,
            payload.external_invoice_ref,
            CREDIT_NOTE_ENDPOINT_NAME,
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

    return _to_out(invoice, branch)


@router.get("/{external_ref}", response_model=SalesInvoiceOut)
async def get_sales_invoice(
    external_ref: str,
    branch_code: str,
    # Defaults to "SALE" for exact backward compatibility — before transaction_kind existed,
    # every row here was implicitly a sale. Pass "CREDIT_NOTE" to look up a credit note by its
    # own external_ref instead (the two are independent idempotency/ref namespaces — see
    # app/db/models.py's SalesInvoice docstring).
    transaction_kind: str = Query(default="SALE"),
    client: Client = Depends(enforce_rate_limit),
    db: AsyncSession = Depends(get_db),
) -> SalesInvoiceOut:
    branch = await get_authorized_branch(db, client, branch_code)

    invoice = await db.scalar(
        select(SalesInvoice).where(
            SalesInvoice.branch_id == branch.id,
            SalesInvoice.external_ref == external_ref,
            SalesInvoice.transaction_kind == transaction_kind,
        )
    )
    if invoice is None:
        raise HTTPException(
            status_code=404, detail=f"No sales invoice found for external_ref={external_ref!r}"
        )
    return _to_out(invoice, branch)
