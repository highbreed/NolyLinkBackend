"""Maps generic ERP/POS client DTOs to KRA request schemas (spec §9.2).

Two kinds of crosswalk are used here, deliberately different:
  - tax_category and product_type map through a small static dict, because spec §7 gives
    their full code lists verbatim (Tax Type A-E, Product Type 1-3) — hardcoding those is
    just encoding the spec, not guessing.
  - unit_of_measure, packaging_unit, origin_nation_cd, and item_classification_cd map through
    *cached* KRA tables (kra_code_lists / kra_item_classes, populated by pollers) instead,
    because their real code lists are never reproduced in the source spec — hardcoding them
    would mean inventing fiscal reference data. This does mean these crosswalks only work once
    the relevant classification has actually been synced from KRA (or a client passes the raw
    KRA code directly, which also matches by `cd`/`item_cls_cd`).

Every resolved code is additionally checked against the submitting Customer's optional
per-classification allow-list (app/services/code_allowlist_service.py) — a no-op for the
(default) unrestricted case, a CodeLookupError if the customer has been configured with an
allow-list that doesn't include this code.
"""

import uuid
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Branch, Item, KraCodeList, KraItemClass, SalesInvoice
from app.schemas.client.credit_note_in import ClientCreditNoteInvoice
from app.schemas.client.item_in import ClientItemIn
from app.schemas.client.sales_in import ClientSalesInvoice, ClientSalesInvoiceLine
from app.schemas.kra.item import ItemSaveReq
from app.schemas.kra.sales import TrnsSalesSaveWrItem, TrnsSalesSaveWrReceipt, TrnsSalesSaveWrReq
from app.services.code_allowlist_service import CodeNotAllowedError, enforce_customer_code_allowlist
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


ITEM_CLASSIFICATION = "item-classification"  # pseudo-slug: item classes have no cd_cls


async def map_item_classification(db: AsyncSession, value: str) -> str:
    """Resolve a friendly value to its KRA item_cls_cd, by exact code or case-insensitive name
    match against the cached KraItemClass table — mirrors map_code_by_name, just against a
    differently-shaped cache (no cd_cls; see KraItemClass's docstring).
    """
    result = await db.execute(select(KraItemClass))
    for row in result.scalars().all():
        if row.item_cls_cd == value or (
            row.item_cls_nm is not None and row.item_cls_nm.casefold() == value.casefold()
        ):
            return row.item_cls_cd
    raise CodeLookupError(
        f"No item classification code matches {value!r} in the cache — sync it first, or pass "
        "the raw KRA item_cls_cd directly."
    )


async def _enforce_allowlist(
    db: AsyncSession, customer_id: uuid.UUID, classification: str, code: str
) -> None:
    try:
        await enforce_customer_code_allowlist(db, customer_id, classification, code)
    except CodeNotAllowedError as exc:
        raise CodeLookupError(str(exc)) from exc


async def map_client_item_to_kra(
    db: AsyncSession,
    branch: Branch,
    payload: ClientItemIn,
    regr_id: str,
    regr_nm: str,
    customer_id: uuid.UUID,
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
        cmcKey=branch.cmc_key,
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

# spec §7, cdCls 37 (Sales Receipt Type): S=Sale, R=Credit Note after Sale — the other of the
# table's only two values, so equally spec-derived as DEFAULT_RCPT_TY_CD above, not guessed.
CREDIT_NOTE_RCPT_TY_CD = "R"


async def map_payment_method(db: AsyncSession, payment_method: str) -> str:
    """spec §7 only gives the first/last of Payment Method's 7 codes ("01 CASH … 07 OTHER"),
    so — like unit_of_measure/packaging_unit — this resolves against the cached code table
    rather than a hardcoded partial map.
    """
    return await map_code_by_name(db, "payment-method", payment_method)


async def map_sales_line(
    db: AsyncSession,
    branch: Branch,
    line: ClientSalesInvoiceLine,
    item_seq: int,
    customer_id: uuid.UUID,
) -> TrnsSalesSaveWrItem:
    item = await db.scalar(select(Item).where(Item.branch_id == branch.id, Item.sku == line.sku))
    if item is None:
        raise CodeLookupError(f"No registered item for sku={line.sku!r} — POST /v1/items first")

    tax_ty_cd = map_tax_category(line.tax_category)
    # tax_category is resupplied per line, independently of the item's own registered
    # tax_ty_cd (unlike UOM/packaging/classification/country, which are read from `item` below
    # and so were already allow-list-checked at registration time) — check it again here or a
    # restricted customer could bypass their tax-type allow-list on a sales/credit-note line.
    await _enforce_allowlist(db, customer_id, "tax-type", tax_ty_cd)
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
    db: AsyncSession,
    branch: Branch,
    payload: ClientSalesInvoice,
    regr_id: str,
    regr_nm: str,
    customer_id: uuid.UUID,
) -> TrnsSalesSaveWrReq:
    pmt_ty_cd = await map_payment_method(db, payload.payment_method)

    item_list = [
        await map_sales_line(db, branch, line, item_seq=i, customer_id=customer_id)
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


class OriginalInvoiceNotFound(CodeLookupError):
    """The credit note's original_invoice_ref doesn't resolve to a fiscalized SalesInvoice on
    this branch — same 422-equivalent handling as any other CodeLookupError.
    """


async def map_client_credit_note_to_kra(
    db: AsyncSession,
    branch: Branch,
    payload: ClientCreditNoteInvoice,
    regr_id: str,
    regr_nm: str,
    customer_id: uuid.UUID,
) -> TrnsSalesSaveWrReq:
    """A credit note is its own fiscal transaction (own invcNo via the same sequence sales
    uses), referencing the original sale via orgInvcNo — not a mutation of the original
    record. See app/schemas/client/credit_note_in.py's module docstring for why rcptTyCd="R"
    (not a separate salesTyCd) is the correct KRA-confirmed shape for this.
    """
    original = await db.scalar(
        select(SalesInvoice).where(
            SalesInvoice.branch_id == branch.id,
            SalesInvoice.external_ref == payload.original_invoice_ref,
            SalesInvoice.transaction_kind == "SALE",
        )
    )
    if original is None or original.kra_invc_no is None:
        raise OriginalInvoiceNotFound(
            f"No fiscalized sales invoice found for original_invoice_ref="
            f"{payload.original_invoice_ref!r} on branch {branch.branch_code!r}"
        )

    rfd_rsn_cd = await map_code_by_name(db, "credit-note-reason", payload.reason)

    item_list = [
        await map_sales_line(db, branch, line, item_seq=i, customer_id=customer_id)
        for i, line in enumerate(payload.lines, start=1)
    ]
    buckets = aggregate_tax_buckets(item_list)

    invc_no = await next_invoice_no(db, branch.tin, branch.bhf_id)
    now = datetime.now()
    cfm_dt = now.strftime("%Y%m%d%H%M%S")
    rfd_dt = now.strftime("%Y%m%d")

    return TrnsSalesSaveWrReq(
        tin=branch.tin,
        bhfId=branch.bhf_id,
        cmcKey=branch.cmc_key,
        trdInvcNo=payload.external_invoice_ref,
        invcNo=invc_no,
        orgInvcNo=str(original.kra_invc_no),
        rcptTyCd=CREDIT_NOTE_RCPT_TY_CD,
        salesSttsCd=DEFAULT_SALES_STTS_CD,
        cfmDt=cfm_dt,
        salesDt=rfd_dt,
        totItemCnt=len(item_list),
        prchrAcptcYn=DEFAULT_PRCHR_ACPTC_YN,
        regrId=regr_id,
        regrNm=regr_nm,
        modrId=regr_id,
        modrNm=regr_nm,
        custTin=payload.customer_pin,
        custNm=payload.customer_name,
        rfdDt=rfd_dt,
        rfdRsnCd=rfd_rsn_cd,
        receipt=TrnsSalesSaveWrReceipt(
            rcptPbctDt=cfm_dt,
            prchrAcptcYn=DEFAULT_PRCHR_ACPTC_YN,
            custTin=payload.customer_pin,
        ),
        itemList=item_list,
        **buckets,
    )
