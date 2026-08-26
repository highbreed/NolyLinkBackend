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

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Branch, Item, KraCodeList
from app.schemas.client.item_in import ClientItemIn
from app.schemas.client.sales_in import ClientSalesInvoice, ClientSalesInvoiceLine
from app.schemas.kra.item import ItemSaveReq
from app.schemas.kra.sales import TrnsSalesSaveWrItem, TrnsSalesSaveWrReceipt, TrnsSalesSaveWrReq
from app.services.code_cache_service import resolve_classification
from app.services.invoice_sequence import next_invoice_no
from app.services.item_code_generator import generate_item_cd

CENTS = Decimal("0.01")


def _round(value: Decimal) -> Decimal:
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


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


# spec §7, cdCls 04 rates — needed for tax-bucket computation as well as code mapping.
TAX_TY_CD_RATE: dict[str, Decimal] = {
    "A": Decimal("0.00"),  # Exempt
    "B": Decimal("16.00"),  # Standard rate
    "C": Decimal("0.00"),  # Zero-rated
    "D": Decimal("0.00"),  # Non-VAT
    "E": Decimal("8.00"),
}

# Fields spec §6.6 requires but that neither the client DTO (spec §9.2) nor KRA supply values
# for on a plain new-sale submission. rcptTyCd (S=Sale/R=Credit Note) is fully given by spec
# §7's Sales Receipt Type table, so "S" is spec-derived, not guessed. salesSttsCd's code list
# (Transaction Progress, cdCls 11) is only partially given ("01 Wait for Approval … 06
# Transferred") — DEFAULT_SALES_STTS_CD is a best-effort default pending sandbox confirmation.
DEFAULT_RCPT_TY_CD = "S"
DEFAULT_SALES_STTS_CD = "02"
DEFAULT_PRCHR_ACPTC_YN = "N"


async def map_payment_method(db: AsyncSession, payment_method: str) -> str:
    """spec §7 only gives the first/last of Payment Method's 7 codes ("01 CASH … 07 OTHER"),
    so — like unit_of_measure/packaging_unit — this resolves against the cached code table
    rather than a hardcoded partial map.
    """
    return await map_code_by_name(db, "payment-method", payment_method)


async def map_sales_line(
    db: AsyncSession, branch: Branch, line: ClientSalesInvoiceLine, item_seq: int
) -> TrnsSalesSaveWrItem:
    item = await db.scalar(select(Item).where(Item.branch_id == branch.id, Item.sku == line.sku))
    if item is None:
        raise CodeLookupError(f"No registered item for sku={line.sku!r} — POST /v1/items first")

    tax_ty_cd = map_tax_category(line.tax_category)
    rate = TAX_TY_CD_RATE[tax_ty_cd]

    sply_amt = _round(line.quantity * line.unit_price)
    dc_amt = _round(sply_amt * line.discount_pct / 100)
    taxbl_amt = sply_amt - dc_amt
    tax_amt = _round(taxbl_amt * rate / 100)
    tot_amt = taxbl_amt + tax_amt

    return TrnsSalesSaveWrItem(
        itemSeq=item_seq,
        itemCd=item.item_cd,
        itemNm=line.description,
        pkgUnitCd=item.pkg_unit_cd,
        pkg=line.quantity,
        qtyUnitCd=item.qty_unit_cd,
        qty=line.quantity,
        prc=line.unit_price,
        splyAmt=sply_amt,
        dcRt=line.discount_pct,
        dcAmt=dc_amt,
        taxTyCd=tax_ty_cd,
        taxblAmt=taxbl_amt,
        totTaxAmt=tax_amt,
        totAmt=tot_amt,
    )


def aggregate_tax_buckets(items: list[TrnsSalesSaveWrItem]) -> dict[str, Decimal]:
    """Sums line items into taxblAmtA..E / taxAmtA..E by tax type, plus taxRtA..E and the
    three grand totals (spec §6.6). Every bucket is populated (0.00 if unused) since these are
    all required header fields, not optional ones.
    """
    buckets: dict[str, Decimal] = {}
    for suffix in "ABCDE":
        buckets[f"taxblAmt{suffix}"] = sum(
            (i.taxblAmt for i in items if i.taxTyCd == suffix), Decimal("0.00")
        )
        buckets[f"taxAmt{suffix}"] = sum(
            (i.totTaxAmt for i in items if i.taxTyCd == suffix), Decimal("0.00")
        )
        buckets[f"taxRt{suffix}"] = TAX_TY_CD_RATE[suffix]

    tot_taxbl_amt = sum((buckets[f"taxblAmt{s}"] for s in "ABCDE"), Decimal("0.00"))
    tot_tax_amt = sum((buckets[f"taxAmt{s}"] for s in "ABCDE"), Decimal("0.00"))
    buckets["totTaxblAmt"] = tot_taxbl_amt
    buckets["totTaxAmt"] = tot_tax_amt
    buckets["totAmt"] = tot_taxbl_amt + tot_tax_amt
    return buckets


async def map_client_sales_invoice_to_kra(
    db: AsyncSession, branch: Branch, payload: ClientSalesInvoice, regr_id: str, regr_nm: str
) -> TrnsSalesSaveWrReq:
    pmt_ty_cd = await map_payment_method(db, payload.payment_method)

    item_list = [
        await map_sales_line(db, branch, line, item_seq=i)
        for i, line in enumerate(payload.lines, start=1)
    ]
    buckets = aggregate_tax_buckets(item_list)

    invc_no = await next_invoice_no(db, branch.tin, branch.bhf_id)
    now = datetime.now()
    cfm_dt = now.strftime("%Y%m%d%H%M%S")
    sales_dt = payload.invoice_date.strftime("%Y%m%d")

    return TrnsSalesSaveWrReq(
        tin=branch.tin,
        bhfId=branch.bhf_id,
        cmcKey=branch.cmc_key,
        trdInvcNo=payload.external_invoice_ref,
        invcNo=invc_no,
        orgInvcNo="0",
        rcptTyCd=DEFAULT_RCPT_TY_CD,
        salesSttsCd=DEFAULT_SALES_STTS_CD,
        cfmDt=cfm_dt,
        salesDt=sales_dt,
        totItemCnt=len(item_list),
        prchrAcptcYn=DEFAULT_PRCHR_ACPTC_YN,
        regrId=regr_id,
        regrNm=regr_nm,
        modrId=regr_id,
        modrNm=regr_nm,
        custTin=payload.customer_pin,
        custNm=payload.customer_name,
        pmtTyCd=pmt_ty_cd,
        receipt=TrnsSalesSaveWrReceipt(
            rcptPbctDt=cfm_dt,
            prchrAcptcYn=DEFAULT_PRCHR_ACPTC_YN,
            custTin=payload.customer_pin,
        ),
        itemList=item_list,
        **buckets,
    )
