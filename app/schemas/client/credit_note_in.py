"""Simplified inbound credit-note DTO for SAP/other ERPs — the counterpart to sales_in.py's
ClientSalesInvoice, for reversing/crediting a prior sale.

KRA's own model (confirmed via KRA_OSCU_FastAPI_Integration_Spec.md §7's code catalogue,
canonical for both OSCU and VSCU per that catalogue being shared business-code semantics, not
a per-provider thing): a credit note is a new fiscal transaction in its own right — rcptTyCd="R"
(cdCls 37: S=Sale, R=Credit Note after Sale) on a normal-type transaction (salesTyCd stays
"N" — Transaction Type cdCls 14's C/N/P/T is orthogonal to Sale/Credit-Note; "NR" is a
confirmed-valid combination per decompiled TrnsSalesExcute.saveTrnsSales's own validation),
referencing the original sale via orgInvcNo. There is no "mutate the original invoice"
operation in either protocol's confirmed surface.
"""

from pydantic import BaseModel

from app.schemas.client.sales_in import ClientSalesInvoiceLine


class ClientCreditNoteInvoice(BaseModel):
    branch_code: str
    # Idempotency key for THIS credit note — distinct from the original sale's own ref below.
    external_invoice_ref: str
    # The original sale's external_invoice_ref — resolves orgInvcNo via the SalesInvoice table.
    original_invoice_ref: str
    # Credit Note Reason (cdCls 32): "01 Missing Quantity … 06 Refund" — resolved against the
    # cached code table like payment_method already is, not hardcoded (spec gives only the
    # first/last of 6 codes, same partial-table situation as payment_method).
    reason: str
    customer_pin: str | None = None
    customer_name: str | None = None
    lines: list[ClientSalesInvoiceLine]
