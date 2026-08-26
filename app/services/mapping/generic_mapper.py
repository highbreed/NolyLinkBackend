"""Maps generic ERP/POS client DTOs to KRA request schemas (spec §9.2).

Two kinds of crosswalk are used here, deliberately different:
  - tax_category and product_type map through a small static dict, because spec §7 gives
    their full code lists verbatim (Tax Type A-E, Product Type 1-3) — hardcoding those is
    just encoding the spec, not guessing.
  - unit_of_measure and packaging_unit map through the *cached* KRA code table
    (kra_code_lists, populated by Phase 5's poller) instead, because their real code lists
    (~43 quantity units, ~52 packaging units) are never reproduced in the source spec —
    hardcoding them would mean inventing fiscal reference data. This does mean these two
    crosswalks only work once the relevant classification has actually been synced from KRA
    (or a client passes the raw KRA code directly, which also matches by `cd`).
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Branch, KraCodeList
from app.schemas.client.item_in import ClientItemIn
from app.schemas.kra.item import ItemSaveReq
from app.services.code_cache_service import resolve_classification
from app.services.item_code_generator import generate_item_cd


class CodeLookupError(ValueError):
    """A client-supplied friendly value has no match — either it's wrong, or the relevant
    classification hasn't been synced from KRA yet (Phase 5).
    """


# spec §7, cdCls 04 (Tax Type): A=Exempt, B=16%, C=0%, D=Non-VAT, E=8%.
TAX_CATEGORY_TO_TAX_TY_CD: dict[str, str] = {
    "EXEMPT": "A",
    "STANDARD16": "B",
    "ZERO": "C",
    "NONVAT": "D",
    "8PCT": "E",
}

# spec §7, cdCls 24 (Product Type): 1=Raw Material, 2=Finished Product, 3=Service.
PRODUCT_TYPE_TO_ITEM_TY_CD: dict[str, str] = {
    "RAW_MATERIAL": "1",
    "FINISHED_PRODUCT": "2",
    "SERVICE": "3",
}


def map_tax_category(tax_category: str) -> str:
    try:
        return TAX_CATEGORY_TO_TAX_TY_CD[tax_category.upper()]
    except KeyError:
        raise CodeLookupError(
            f"Unknown tax_category {tax_category!r}; expected one of "
            f"{sorted(TAX_CATEGORY_TO_TAX_TY_CD)}"
        ) from None


def map_product_type(product_type: str) -> str:
    try:
        return PRODUCT_TYPE_TO_ITEM_TY_CD[product_type.upper()]
    except KeyError:
        raise CodeLookupError(
            f"Unknown product_type {product_type!r}; expected one of "
            f"{sorted(PRODUCT_TYPE_TO_ITEM_TY_CD)}"
        ) from None


async def map_code_by_name(db: AsyncSession, classification: str, value: str) -> str:
    """Resolve a friendly value to its KRA cd, by exact cd or case-insensitive cd_nm match
    against the cached code table.
    """
    cd_cls = resolve_classification(classification)
    result = await db.execute(select(KraCodeList).where(KraCodeList.cd_cls == cd_cls))
    for row in result.scalars().all():
        if row.cd == value or (row.cd_nm is not None and row.cd_nm.casefold() == value.casefold()):
            return row.cd
    raise CodeLookupError(
        f"No {classification!r} code matches {value!r} in the cache — sync it via Phase 5's "
        "poller first, or pass the raw KRA code directly."
    )


async def map_client_item_to_kra(
    db: AsyncSession,
    branch: Branch,
    payload: ClientItemIn,
    regr_id: str,
    regr_nm: str,
    existing_item_cd: str | None = None,
) -> tuple[ItemSaveReq, str]:
    """Returns the mapped KRA request and the itemCd (also embedded in the request), since
    callers persist both. Pass existing_item_cd when updating an item the gateway already
    registered — saveItem is "create/update" (spec §18), and re-registering a previously
    assigned itemCd under a fresh one would create a duplicate at KRA, not an update.
    """
    tax_ty_cd = map_tax_category(payload.tax_category)
    item_ty_cd = map_product_type(payload.product_type)
    pkg_unit_cd = await map_code_by_name(db, "packaging-unit", payload.packaging_unit)
    qty_unit_cd = await map_code_by_name(db, "quantity-unit", payload.unit_of_measure)

    item_cd = existing_item_cd or await generate_item_cd(
        db,
        tin=branch.tin,
        bhf_id=branch.bhf_id,
        orgn_nat_cd=payload.origin_nation_cd,
        item_ty_cd=item_ty_cd,
        pkg_unit_cd=pkg_unit_cd,
        qty_unit_cd=qty_unit_cd,
    )

    req = ItemSaveReq(
        tin=branch.tin,
        bhfId=branch.bhf_id,
        cmcKey=branch.cmc_key,
        itemClsCd=payload.item_classification_cd,
        itemCd=item_cd,
        itemTyCd=item_ty_cd,
        itemNm=payload.name,
        orgnNatCd=payload.origin_nation_cd,
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
