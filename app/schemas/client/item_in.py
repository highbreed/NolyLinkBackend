"""Simplified inbound item DTO for SAP/other ERPs (spec §9.2).

Client systems shouldn't need to know KRA's field names (itemClsCd, pkgUnitCd, ...) — the
mapper in app/services/mapping/generic_mapper.py converts this into ItemSaveReq. There's no
friendly crosswalk for item_classification_cd or origin_nation_cd in the source spec (unlike
tax_category/product_type/unit_of_measure/packaging_unit), so both are accepted as raw KRA
codes directly rather than friendly names — but, like unit_of_measure/packaging_unit, they are
still validated against a cache (KraItemClass / the "nation" classification) by the mapper, not
passed through unchecked.
"""

from decimal import Decimal

from pydantic import BaseModel


class ClientItemIn(BaseModel):
    branch_code: str
    sku: str
    name: str
    item_classification_cd: str
    unit_of_measure: str  # -> qtyUnitCd via the cached Unit of Quantity table
    packaging_unit: str  # -> pkgUnitCd via the cached Packaging Unit table
    product_type: str  # "RAW_MATERIAL" | "FINISHED_PRODUCT" | "SERVICE" -> itemTyCd
    tax_category: str  # "STANDARD16" | "ZERO" | "EXEMPT" | "NONVAT" | "8PCT" -> taxTyCd
    unit_price: Decimal
    origin_nation_cd: str = "KE"
    insurance_applicable: bool = False
