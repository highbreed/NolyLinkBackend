"""Simplified inbound stock movement/master DTOs for SAP/other ERPs."""

from decimal import Decimal

from pydantic import BaseModel


class ClientStockIOLine(BaseModel):
    sku: str
    quantity: Decimal
    unit_price: Decimal


class ClientStockIOIn(BaseModel):
    branch_code: str
    external_ref: str
    # Stock In/Out (spec §7, cdCls 12): 01-06 incoming, 11-16 outgoing — only the ranges are
    # given, no per-code meanings, so this is the raw KRA code, required from the caller.
    sar_ty_cd: str
    lines: list[ClientStockIOLine]


class ClientStockMasterIn(BaseModel):
    branch_code: str
    sku: str
    remaining_quantity: Decimal
