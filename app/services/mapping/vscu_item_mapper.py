"""Maps the same client-facing ClientItemIn (app/schemas/client/item_in.py) to VSCU's
ItemSaveReq — the VSCU counterpart to app/services/mapping/generic_mapper.py's
map_client_item_to_kra, closing the gap where item registration only worked for OSCU branches
(sales/credit-notes already dispatch by device_type via vscu_sales_mapper.py; this is that
same pattern for items).

Reuses everything that genuinely IS the same KRA fact regardless of transport: the tax/
product-type crosswalks, the packaging/quantity/nation code-cache lookups, item-classification
lookup, the per-customer code allow-list enforcement, and itemCd generation (spec §8's
sequence is keyed by (tin, bhfId, ...), not by protocol). VSCU's ItemSaveReq has no cmcKey
(VSCURequestBase carries only tin/bhfId — the local VSCU app owns its own trust anchor), which
is the one real shape difference from OSCU's ItemSaveReq.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Branch
from app.integrations.vscu.schemas.item import ItemSaveReq
from app.schemas.client.item_in import ClientItemIn
from app.services.item_code_generator import generate_item_cd
from app.services.mapping.generic_mapper import (
    ITEM_CLASSIFICATION,
    _enforce_allowlist,
    map_code_by_name,
    map_item_classification,
    map_product_type,
    map_tax_category,
)


async def map_client_item_to_vscu(
    db: AsyncSession,
    branch: Branch,
    payload: ClientItemIn,
    regr_id: str,
    regr_nm: str,
    customer_id: uuid.UUID,
    existing_item_cd: str | None = None,
) -> tuple[ItemSaveReq, str]:
    """Returns the mapped VSCU request and the itemCd (also embedded in the request) — same
    contract as map_client_item_to_kra. Pass existing_item_cd when updating an item the
    gateway already registered, for the same reason that function does.
    """
    tax_ty_cd = map_tax_category(payload.tax_category)
    item_ty_cd = map_product_type(payload.product_type)
    pkg_unit_cd = await map_code_by_name(db, "packaging-unit", payload.packaging_unit)
    qty_unit_cd = await map_code_by_name(db, "quantity-unit", payload.unit_of_measure)
    orgn_nat_cd = await map_code_by_name(db, "nation", payload.origin_nation_cd)
    item_cls_cd = await map_item_classification(db, payload.item_classification_cd)

    await _enforce_allowlist(db, customer_id, "tax-type", tax_ty_cd)
    await _enforce_allowlist(db, customer_id, "packaging-unit", pkg_unit_cd)
    await _enforce_allowlist(db, customer_id, "quantity-unit", qty_unit_cd)
    await _enforce_allowlist(db, customer_id, "nation", orgn_nat_cd)
    await _enforce_allowlist(db, customer_id, ITEM_CLASSIFICATION, item_cls_cd)

    item_cd = existing_item_cd or await generate_item_cd(
        db,
        tin=branch.tin,
        bhf_id=branch.bhf_id,
        orgn_nat_cd=orgn_nat_cd,
        item_ty_cd=item_ty_cd,
        pkg_unit_cd=pkg_unit_cd,
        qty_unit_cd=qty_unit_cd,
    )

    req = ItemSaveReq(
        tin=branch.tin,
        bhfId=branch.bhf_id,
        itemClsCd=item_cls_cd,
        itemCd=item_cd,
        itemTyCd=item_ty_cd,
        itemNm=payload.name,
        orgnNatCd=orgn_nat_cd,
        pkgUnitCd=pkg_unit_cd,
        qtyUnitCd=qty_unit_cd,
        taxTyCd=tax_ty_cd,
        dftPrc=payload.unit_price,
        isrcAplcbYn="Y" if payload.insurance_applicable else "N",
        useYn="Y",
        regrId=regr_id,
        regrNm=regr_nm,
        modrId=regr_id,
        modrNm=regr_nm,
    )
    return req, item_cd
