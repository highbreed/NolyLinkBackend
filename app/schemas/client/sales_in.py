"""Simplified inbound sales invoice DTO for SAP/other ERPs (spec §9.2, given verbatim there).

sku must already be registered via POST /v1/items (Phase 6) — this DTO has no packaging
field, so pkgUnitCd/qtyUnitCd for the KRA line item come from the registered Item, not the
client (see app/services/mapping/generic_mapper.py).
"""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel


class ClientSalesInvoiceLine(BaseModel):
    sku: str
    description: str
    quantity: Decimal
    unit_of_measure: str  # mapped via UoM crosswalk table -> qtyUnitCd
    unit_price: Decimal
    discount_pct: Decimal = Decimal("0")
    tax_category: str  # "STANDARD16" | "ZERO" | "EXEMPT" | "NONVAT" | "8PCT" -> taxTyCd


class ClientSalesInvoice(BaseModel):
    branch_code: str  # our internal branch id -> resolves (tin, bhfId, cmcKey)
    external_invoice_ref: str  # idempotency key, e.g. SAP billing doc number
    customer_pin: str | None = None
    customer_name: str | None = None
    payment_method: str  # "CASH" | "CREDIT" | ... -> pmtTyCd
    invoice_date: date
    lines: list[ClientSalesInvoiceLine]
