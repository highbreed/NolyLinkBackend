"""Simplified inbound purchase DTO for SAP/other ERPs.

Confirms a supplier-declared sale the gateway already cached from
/selectTrnsPurchaseSalesList (GET /v1/purchases/pending) — the client references it by
(spplr_tin, spplr_invc_no) rather than resupplying amounts/line items KRA already told us
about.
"""

from pydantic import BaseModel


class ClientPurchaseIn(BaseModel):
    branch_code: str
    external_ref: str
    spplr_tin: str
    spplr_invc_no: str
    # Transaction Type (spec §7, cdCls 14): the spec gives only the bare codes "C/N/P/T" with
    # no attached meanings anywhere in the source document — unlike Registration Type or
    # Purchase Receipt Type (both fully labeled A-Z with descriptions), there's nothing here
    # to crosswalk against, so this is the raw KRA code, required directly from the caller.
    pchs_ty_cd: str
