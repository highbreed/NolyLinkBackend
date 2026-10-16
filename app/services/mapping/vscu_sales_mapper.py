"""Maps the same client-facing ClientSalesInvoice (app/schemas/client/sales_in.py) to VSCU's
wire DTOs — the VSCU counterpart to app/services/mapping/generic_mapper.py's
map_client_sales_invoice_to_kra.

Deliberately NOT a thin wrapper around generic_mapper's OSCU output: VSCU's TrnsSalesSaveWrItem
names its line-level tax amount `taxAmt`, not `totTaxAmt` (see
app/integrations/vscu/schemas/sales.py's module docstring — a confirmed, not assumed,
difference), and its header requires `salesTyCd`, which OSCU has no equivalent of. Reuses
everything that genuinely IS the same KRA fact regardless of transport: the tax-category
crosswalk, tax rates, rounding, payment-method crosswalk, and the per-branch invoice-number
sequence (a branch is always exclusively OSCU or VSCU, so there's no risk of the two
transports racing over the same sequence).
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Branch, Item, SalesInvoice
from app.integrations.vscu.schemas.sales import (
    TrnsSalesSaveWrItem,
    TrnsSalesSaveWrReceipt,
    TrnsSalesSaveWrReq,
)
from app.schemas.client.credit_note_in import ClientCreditNoteInvoice
from app.schemas.client.sales_in import ClientSalesInvoice, ClientSalesInvoiceLine
from app.services.invoice_sequence import next_invoice_no
from app.services.mapping.generic_mapper import (
    TAX_TY_CD_RATE,
    CodeLookupError,
    OriginalInvoiceNotFound,
    _round,
    map_code_by_name,
    map_payment_method,
    map_tax_category,
)

# "N"=Normal / "S"=Sale — the standard case for a plain new invoice (verified valid
# combination against TrnsSalesExcute.saveTrnsSales's own 834 validation: NS/NR/TS/TR/CS/CR/PS).
DEFAULT_SALES_TY_CD = "N"
DEFAULT_RCPT_TY_CD = "S"
DEFAULT_SALES_STTS_CD = "02"
DEFAULT_PRCHR_ACPTC_YN = "N"

# spec §7, cdCls 37: S=Sale, R=Credit Note after Sale — same catalogue OSCU's
# CREDIT_NOTE_RCPT_TY_CD uses (app/services/mapping/generic_mapper.py), treated as canonical
# business-code semantics shared by both providers, not a per-provider guess. "NR" (this value
# + DEFAULT_SALES_TY_CD="N") is confirmed valid by decompiled TrnsSalesExcute's own validation.
CREDIT_NOTE_RCPT_TY_CD = "R"


async def map_sales_line_to_vscu(
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
        itemClsCd=item.item_cls_cd,
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
        taxAmt=tax_amt,
        totAmt=tot_amt,
    )


def aggregate_tax_buckets_vscu(items: list[TrnsSalesSaveWrItem]) -> dict[str, Decimal]:
    """Same math as generic_mapper.aggregate_tax_buckets, over VSCU's own item type (whose
    per-line tax field is `taxAmt`, not `totTaxAmt` — see this module's docstring). Every
    bucket is populated (0.00 if unused) since these are all required header fields.
    """
    buckets: dict[str, Decimal] = {}
    for suffix in "ABCDE":
        buckets[f"taxblAmt{suffix}"] = sum(
            (i.taxblAmt for i in items if i.taxTyCd == suffix), Decimal("0.00")
        )
        buckets[f"taxAmt{suffix}"] = sum(
            (i.taxAmt for i in items if i.taxTyCd == suffix), Decimal("0.00")
        )
        buckets[f"taxRt{suffix}"] = TAX_TY_CD_RATE[suffix]

    tot_taxbl_amt = sum((buckets[f"taxblAmt{s}"] for s in "ABCDE"), Decimal("0.00"))
    tot_tax_amt = sum((buckets[f"taxAmt{s}"] for s in "ABCDE"), Decimal("0.00"))
    buckets["totTaxblAmt"] = tot_taxbl_amt
    buckets["totTaxAmt"] = tot_tax_amt
    buckets["totAmt"] = tot_taxbl_amt + tot_tax_amt
    return buckets


async def map_client_sales_invoice_to_vscu(
    db: AsyncSession, branch: Branch, payload: ClientSalesInvoice, regr_id: str, regr_nm: str
) -> TrnsSalesSaveWrReq:
    pmt_ty_cd = await map_payment_method(db, payload.payment_method)

    item_list = [
        await map_sales_line_to_vscu(db, branch, line, item_seq=i)
        for i, line in enumerate(payload.lines, start=1)
    ]
    buckets = aggregate_tax_buckets_vscu(item_list)

    invc_no = int(await next_invoice_no(db, branch.tin, branch.bhf_id))
    now = datetime.now()
    cfm_dt = now.strftime("%Y%m%d%H%M%S")
    sales_dt = payload.invoice_date.strftime("%Y%m%d")

    return TrnsSalesSaveWrReq(
        tin=branch.tin,
        bhfId=branch.bhf_id,
        invcNo=invc_no,
        orgInvcNo=0,
        salesTyCd=DEFAULT_SALES_TY_CD,
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
            custTin=payload.customer_pin,
            prchrAcptcYn=DEFAULT_PRCHR_ACPTC_YN,
        ),
        itemList=item_list,
        **buckets,
    )


async def map_client_credit_note_to_vscu(
    db: AsyncSession, branch: Branch, payload: ClientCreditNoteInvoice, regr_id: str, regr_nm: str
) -> TrnsSalesSaveWrReq:
    """VSCU counterpart to generic_mapper.map_client_credit_note_to_kra — see that function
    and app/schemas/client/credit_note_in.py's module docstring for why this is rcptTyCd="R"
    on a new transaction referencing orgInvcNo, not a mutation of the original.
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
        await map_sales_line_to_vscu(db, branch, line, item_seq=i)
        for i, line in enumerate(payload.lines, start=1)
    ]
    buckets = aggregate_tax_buckets_vscu(item_list)

    invc_no = int(await next_invoice_no(db, branch.tin, branch.bhf_id))
    now = datetime.now()
    cfm_dt = now.strftime("%Y%m%d%H%M%S")
    rfd_dt = now.strftime("%Y%m%d")

    return TrnsSalesSaveWrReq(
        tin=branch.tin,
        bhfId=branch.bhf_id,
        invcNo=invc_no,
        orgInvcNo=original.kra_invc_no,
        salesTyCd=DEFAULT_SALES_TY_CD,
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
            custTin=payload.customer_pin,
            prchrAcptcYn=DEFAULT_PRCHR_ACPTC_YN,
        ),
        itemList=item_list,
        **buckets,
    )
