"""VSCUSalesService: the VSCU counterpart to app/api/v1/endpoints/sales.py's OSCU inline
flow, for both a normal sale (save_sale) and a credit note (save_credit_note) — both share the
exact same idempotency mechanism (app/services/idempotency.py), persistence table
(SalesInvoice — same fiscal-record shape, protocol-agnostic), and submission/error-handling
flow (_submit_via_mapper), since only how the outbound TrnsSalesSaveWrReq gets built differs
between them (app/services/mapping/vscu_sales_mapper.py's two mapper functions). That flow
differs meaningfully from OSCU's inline one because the local VSCU app does local work OSCU's
KRA endpoint never does. Four cases, each handled differently in _submit_via_mapper:

1. resultCd=="000" (whether truly transmitted, or locally signed-and-queued with
   resultMsg="INVOICE IS SIGNED BUT NOT TRANSMITTED" — see client.save_sales's docstring,
   verified against decompiled TrnsSalesExcute.saveTrnsSales): a genuine, signed fiscal
   receipt either way. Persisted as FISCALIZED (transmitted) or SIGNED_PENDING_TRANSMISSION
   (queued), never treated as a failure.
2. 836 (VSCUSequenceError): the local app's sequence counters are out of step. Per spec §9's
   reconciliation flow — stop, don't blindly retry. The idempotency claim is released (not
   stored as a failure) so the *same* external_ref can be retried once ops has resynced
   sequences (POST /v1/vscu/{branch_id}/sequences/sync), rather than being permanently stuck.
3. 924 (VSCUDuplicateSubmissionError, "INVOICE NUMBER IS ALREADY PROCESSED"): this is one of
   two ambiguous-result cases (spec §26) — it means the local app already has this exact
   invcNo on file, which usually means a *prior* attempt actually succeeded and we simply
   never got to persist it (e.g. a crash between VSCU accepting and our own write).
4. VSCUTransportError (our own connection to the local VSCU app itself failed/timed out —
   distinct from case 3: here we never even got an HTTP response, so we don't know whether
   the local app received and processed the request before the connection dropped) — the
   other ambiguous-result case spec §26 describes explicitly ("a network timeout after
   sending /saveSales is an ambiguous result, not automatically a failed transaction").

Cases 3 and 4 both reconcile via reconcile_by_invoice_number() (POST /trnsSales/
selectInvoiceDetails for the invcNo we attempted) before giving up — if VSCU confirms the
invoice exists, treat as success; only if reconciliation itself can't confirm either way does
the original error propagate, with the idempotency claim released so it can be retried.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Branch, SalesInvoice
from app.integrations.vscu.client import VSCUClient
from app.integrations.vscu.exceptions import (
    VSCUDuplicateSubmissionError,
    VSCUError,
    VSCUSequenceError,
    VSCUTransportError,
)
from app.integrations.vscu.schemas.sales import (
    InvoiceDetailsReq,
    TrnsSalesSaveWrReq,
    TrnsSalesSaveWrRes,
    TrnsSalesSaveWrResData,
)
from app.schemas.client.credit_note_in import ClientCreditNoteInvoice
from app.schemas.client.sales_in import ClientSalesInvoice
from app.services import idempotency
from app.services.mapping.generic_mapper import CodeLookupError
from app.services.mapping.vscu_sales_mapper import (
    map_client_credit_note_to_vscu,
    map_client_sales_invoice_to_vscu,
)

logger = logging.getLogger(__name__)

ENDPOINT_NAME = "/v1/sales-invoices"
CREDIT_NOTE_ENDPOINT_NAME = "/v1/sales-invoices/credit-notes"

STATUS_FISCALIZED = "FISCALIZED"
STATUS_SIGNED_PENDING_TRANSMISSION = "SIGNED_PENDING_TRANSMISSION"

_TRANSMITTED_MESSAGE = "The request has been processed successfully"


class VSCUSequenceErrorNeedsResync(Exception):
    """836 surfaced to the caller — the idempotency claim has already been released so the
    same external_ref is safe to retry once ops has called sequences/sync.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


class VSCUSalesService:
    def __init__(self, db: AsyncSession, client: VSCUClient) -> None:
        self._db = db
        self._client = client

    async def save_sale(
        self,
        client_id: uuid.UUID,
        branch: Branch,
        payload: ClientSalesInvoice,
        regr_id: str = "gateway",
        regr_nm: str = "VSCU Gateway",
    ) -> tuple[SalesInvoice, dict[str, object]]:
        """Returns (invoice, kra_response_dict). Raises CodeLookupError (422-equivalent),
        VSCUSequenceErrorNeedsResync (409-equivalent, retryable), or lets VSCUError/
        VSCUDuplicateSubmissionError (that reconciliation couldn't resolve) propagate as a
        502-equivalent — callers map these the same way app/api/v1/endpoints/sales.py already
        maps KraError today.
        """
        return await self._submit_via_mapper(
            client_id,
            branch,
            payload.external_invoice_ref,
            ENDPOINT_NAME,
            "SALE",
            None,
            lambda: map_client_sales_invoice_to_vscu(
                self._db, branch, payload, regr_id=regr_id, regr_nm=regr_nm
            ),
        )

    async def save_credit_note(
        self,
        client_id: uuid.UUID,
        branch: Branch,
        payload: ClientCreditNoteInvoice,
        regr_id: str = "gateway",
        regr_nm: str = "VSCU Gateway",
    ) -> tuple[SalesInvoice, dict[str, object]]:
        """Same flow as save_sale (idempotency, ambiguous-result reconciliation, persistence)
        — only how the request is built differs (map_client_credit_note_to_vscu, which
        resolves orgInvcNo from the original sale via payload.original_invoice_ref and sets
        rcptTyCd="R" — see that mapper and app/schemas/client/credit_note_in.py for why this
        is a new fiscal transaction, not a mutation of the original invoice). Raises
        OriginalInvoiceNotFound (a CodeLookupError subclass, 422-equivalent) if
        original_invoice_ref doesn't resolve to a fiscalized SalesInvoice on this branch.
        """
        return await self._submit_via_mapper(
            client_id,
            branch,
            payload.external_invoice_ref,
            CREDIT_NOTE_ENDPOINT_NAME,
            "CREDIT_NOTE",
            payload.original_invoice_ref,
            lambda: map_client_credit_note_to_vscu(
                self._db, branch, payload, regr_id=regr_id, regr_nm=regr_nm
            ),
        )

    async def _submit_via_mapper(
        self,
        client_id: uuid.UUID,
        branch: Branch,
        external_ref: str,
        endpoint_name: str,
        transaction_kind: str,
        original_ref: str | None,
        build_req: Callable[[], Awaitable[TrnsSalesSaveWrReq]],
    ) -> tuple[SalesInvoice, dict[str, object]]:
        cached = await idempotency.claim_or_get_cached(
            self._db, client_id, external_ref, endpoint_name
        )
        if cached is not None:
            invoice = await self._db.scalar(
                select(SalesInvoice).where(
                    SalesInvoice.branch_id == branch.id,
                    SalesInvoice.external_ref == external_ref,
                    SalesInvoice.transaction_kind == transaction_kind,
                )
            )
            if invoice is None:
                # Idempotency row exists but our own SalesInvoice write never landed (should
                # not happen in steady state — both are written in the same transaction below
                # — but treated defensively rather than crashing the cached-return path).
                raise RuntimeError(
                    f"idempotency cache hit for external_ref={external_ref!r} but no matching "
                    "SalesInvoice row exists"
                )
            return invoice, cached

        try:
            req = await build_req()
        except CodeLookupError:
            await idempotency.release_claim(self._db, client_id, external_ref, endpoint_name)
            raise

        try:
            res = await self._client.save_sales(branch.id, req)
        except VSCUSequenceError as exc:
            await idempotency.release_claim(self._db, client_id, external_ref, endpoint_name)
            raise VSCUSequenceErrorNeedsResync(
                f"VSCU sequence error for branch {branch.id}: {exc.context.result_msg}. "
                f"Resync via POST /v1/vscu/{branch.id}/sequences/sync, then retry."
            ) from exc
        except (VSCUDuplicateSubmissionError, VSCUTransportError):
            # Both are ambiguous-result cases (spec §26) — see module docstring cases 3/4.
            reconciled = await self.reconcile_by_invoice_number(branch, req.invcNo)
            if reconciled is None:
                # Reconciliation couldn't confirm it either — a genuinely unresolved
                # ambiguous state. Release the claim (not store a failure) so this can be
                # retried/investigated rather than permanently wedged.
                await idempotency.release_claim(self._db, client_id, external_ref, endpoint_name)
                raise
            res = reconciled
        except VSCUError:
            # Every other VSCUError (834 bad salesTyCd/rcptTyCd, 899 local exception, a
            # transport failure, ...) is a genuine, non-ambiguous rejection — release the
            # claim so the same external_ref can be corrected and resubmitted, same as
            # app/api/v1/endpoints/sales.py already does for KraError on the OSCU side.
            await idempotency.release_claim(self._db, client_id, external_ref, endpoint_name)
            raise

        if res.data is None:
            await idempotency.release_claim(self._db, client_id, external_ref, endpoint_name)
            raise RuntimeError("VSCU returned resultCd=000 for /saveSales with no receipt data")

        status = (
            STATUS_SIGNED_PENDING_TRANSMISSION
            if res.resultMsg and res.resultMsg != _TRANSMITTED_MESSAGE
            else STATUS_FISCALIZED
        )

        invoice = SalesInvoice(
            branch_id=branch.id,
            external_ref=external_ref,
            transaction_kind=transaction_kind,
            original_ref=original_ref,
            kra_invc_no=req.invcNo,
            kra_cur_rcpt_no=res.data.rcptNo,
            kra_tot_rcpt_no=res.data.totRcptNo,
            kra_rcpt_sign=res.data.rcptSign,
            kra_intrl_data=res.data.intrlData,
            kra_sdc_datetime=res.data.vsdcRcptPbctDate,
            kra_sdc_id=res.data.sdcId,
            status=status,
        )
        self._db.add(invoice)

        response_dict: dict[str, object] = {
            "curRcptNo": res.data.rcptNo,
            "totRcptNo": res.data.totRcptNo,
            "rcptSign": res.data.rcptSign,
            "intrlData": res.data.intrlData,
            "sdcDateTime": res.data.vsdcRcptPbctDate,
            "status": status,
        }
        await idempotency.store_response(
            self._db, client_id, external_ref, endpoint_name, response_dict
        )
        return invoice, response_dict

    async def reconcile_by_invoice_number(
        self, branch: Branch, invc_no: int
    ) -> TrnsSalesSaveWrRes | None:
        """General-purpose reconciliation: look up invc_no via /trnsSales/selectInvoiceDetails
        to confirm whether VSCU actually holds a fiscalized invoice under that number, rather
        than assuming either way. Used internally by _submit_via_mapper for both ambiguous-
        result cases (924 duplicate, and a transport failure to the local app itself), and
        safe to call standalone for ops-driven reconciliation (e.g. an admin endpoint) when
        the invcNo a prior attempt used is known. Returns a TrnsSalesSaveWrRes-shaped result
        built from the confirmed invoice details, or None if the lookup can't confirm it
        either way.
        """
        try:
            details = await self._client.select_invoice_details(
                branch.id,
                InvoiceDetailsReq(tin=branch.tin, bhfId=branch.bhf_id, invcNo=str(invc_no)),
            )
        except VSCUError:
            logger.exception(
                "Reconciliation lookup failed for branch=%s invcNo=%s", branch.id, invc_no
            )
            return None

        if details.data is None or not details.data.salesList:
            return None
        matched = next((s for s in details.data.salesList if s.invcNo == invc_no), None)
        if matched is None or matched.receipt is None:
            return None

        # Confirmed present in real KRA's own invoice list — this is strictly stronger proof
        # than the ordinary "000" response (which can itself mean "signed but not yet
        # transmitted"), so it's reported with the exact transmitted-message sentinel to
        # classify as FISCALIZED below, not SIGNED_PENDING_TRANSMISSION.
        receipt = matched.receipt

        # Update any existing SalesInvoice row this confirms — e.g. ops-driven reconciliation
        # (POST /v1/vscu/{branch_id}/sales/{invc_no}/reconcile) run well after the original
        # submission, which previously left a SIGNED_PENDING_TRANSMISSION row stuck that way
        # forever even once KRA had genuinely confirmed transmission. A no-op when called
        # from _submit_via_mapper's own ambiguous-result cases above (that row doesn't exist
        # yet at this point — it's created by the caller right after this returns).
        existing = await self._db.scalar(
            select(SalesInvoice).where(
                SalesInvoice.branch_id == branch.id, SalesInvoice.kra_invc_no == invc_no
            )
        )
        if existing is not None and existing.status != STATUS_FISCALIZED:
            existing.status = STATUS_FISCALIZED
            existing.kra_cur_rcpt_no = int(receipt.curRcptNo) if receipt.curRcptNo else None
            existing.kra_tot_rcpt_no = int(receipt.totRcptNo) if receipt.totRcptNo else None
            existing.kra_rcpt_sign = receipt.rcptSign
            existing.kra_intrl_data = receipt.intrlData
            existing.kra_sdc_datetime = receipt.sdcDateTime or receipt.rcptPbctDt
            await self._db.flush()
            logger.info(
                "Reconciliation confirmed transmission for branch=%s invcNo=%s — "
                "SalesInvoice external_ref=%s updated to FISCALIZED",
                branch.id,
                invc_no,
                existing.external_ref,
            )

        return TrnsSalesSaveWrRes(
            resultCd="000",
            resultMsg=_TRANSMITTED_MESSAGE,
            resultDt=None,
            data=TrnsSalesSaveWrResData(
                rcptNo=int(receipt.curRcptNo) if receipt.curRcptNo else None,
                intrlData=receipt.intrlData,
                rcptSign=receipt.rcptSign,
                totRcptNo=int(receipt.totRcptNo) if receipt.totRcptNo else None,
                vsdcRcptPbctDate=receipt.sdcDateTime or receipt.rcptPbctDt,
                sdcId=None,
                mrcNo=None,
            ),
        )
