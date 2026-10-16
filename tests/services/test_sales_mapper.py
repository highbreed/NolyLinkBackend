"""Tests for the sales invoice mapper (spec §9.2, §6.6) — tax-bucket aggregation is the
highest-stakes logic in the whole gateway (spec §14 calls sales invoices "the fiscal path"),
so this focuses on multiple tax types on one invoice.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.db.models import Branch, Item, KraCodeList
from app.schemas.client.sales_in import ClientSalesInvoice, ClientSalesInvoiceLine
from app.services.mapping.generic_mapper import (
    CodeLookupError,
    aggregate_tax_buckets,
    map_client_sales_invoice_to_kra,
    map_sales_line,
)

# No CustomerCodeAllowlist rows are seeded in this file, so any customer_id is unrestricted
# (see code_allowlist_service.py) — a fixed random id keeps every call site consistent.
CUSTOMER_ID = uuid.uuid4()


@pytest.fixture
async def branch_with_items(db_session) -> Branch:
    branch = Branch(branch_code="NAIROBI-HQ", tin="A123456789Z", bhf_id="00", cmc_key="c" * 10)
    db_session.add(branch)
    db_session.add_all(
        [
            Item(
                branch=branch,
                sku="BREAD",
                item_cd="KE2NTBA0000001",
                item_cls_cd="5020150101",
                item_nm="Bread",
                tax_ty_cd="B",
                dft_prc=Decimal("100.00"),
                pkg_unit_cd="NT",
                qty_unit_cd="BA",
            ),
            Item(
                branch=branch,
                sku="MILK",
                item_cd="KE2NTBA0000002",
                item_cls_cd="5020150102",
                item_nm="Milk",
                tax_ty_cd="C",
                dft_prc=Decimal("50.00"),
                pkg_unit_cd="NT",
                qty_unit_cd="BA",
            ),
            Item(
                branch=branch,
                sku="BOOK",
                item_cd="KE2NTBA0000003",
                item_cls_cd="5020150103",
                item_nm="Book",
                tax_ty_cd="A",
                dft_prc=Decimal("500.00"),
                pkg_unit_cd="NT",
                qty_unit_cd="BA",
            ),
            KraCodeList(cd_cls="07", cd="01", cd_nm="CASH"),
        ]
    )
    await db_session.flush()
    return branch


async def test_tax_buckets_split_across_multiple_tax_types(db_session, branch_with_items):
    lines = [
        ClientSalesInvoiceLine(
            sku="BREAD", description="Bread", quantity=Decimal("2"),
            unit_of_measure="BA", unit_price=Decimal("100.00"), tax_category="STANDARD16",
        ),
        ClientSalesInvoiceLine(
            sku="MILK", description="Milk", quantity=Decimal("3"),
            unit_of_measure="BA", unit_price=Decimal("50.00"), tax_category="ZERO",
        ),
        ClientSalesInvoiceLine(
            sku="BOOK", description="Book", quantity=Decimal("1"),
            unit_of_measure="BA", unit_price=Decimal("500.00"), tax_category="EXEMPT",
        ),
    ]  # fmt: skip

    items = [
        await map_sales_line(
            db_session, branch_with_items, line, item_seq=i, customer_id=CUSTOMER_ID
        )
        for i, line in enumerate(lines, start=1)
    ]
    buckets = aggregate_tax_buckets(items)

    # Bread: 2 * 100 = 200 taxable @ 16% = 32.00 tax
    assert buckets["taxblAmtB"] == Decimal("200.00")
    assert buckets["taxAmtB"] == Decimal("32.00")
    assert buckets["taxRtB"] == Decimal("16.00")

    # Milk: 3 * 50 = 150 taxable @ 0% = 0.00 tax
    assert buckets["taxblAmtC"] == Decimal("150.00")
    assert buckets["taxAmtC"] == Decimal("0.00")

    # Book: 1 * 500 = 500 taxable (exempt) @ 0% = 0.00 tax
    assert buckets["taxblAmtA"] == Decimal("500.00")
    assert buckets["taxAmtA"] == Decimal("0.00")

    # Untouched buckets are still populated at zero, not omitted.
    assert buckets["taxblAmtD"] == Decimal("0.00")
    assert buckets["taxAmtD"] == Decimal("0.00")
    assert buckets["taxblAmtE"] == Decimal("0.00")
    assert buckets["taxAmtE"] == Decimal("0.00")

    assert buckets["totTaxblAmt"] == Decimal("200.00") + Decimal("150.00") + Decimal("500.00")
    assert buckets["totTaxAmt"] == Decimal("32.00")
    assert buckets["totAmt"] == buckets["totTaxblAmt"] + buckets["totTaxAmt"]


async def test_two_lines_same_tax_type_sum_into_one_bucket(db_session, branch_with_items):
    lines = [
        ClientSalesInvoiceLine(
            sku="BREAD", description="Bread", quantity=Decimal("1"),
            unit_of_measure="BA", unit_price=Decimal("100.00"), tax_category="STANDARD16",
        ),
        ClientSalesInvoiceLine(
            sku="BREAD", description="Bread (2nd batch)", quantity=Decimal("1"),
            unit_of_measure="BA", unit_price=Decimal("50.00"), tax_category="STANDARD16",
        ),
    ]  # fmt: skip

    items = [
        await map_sales_line(
            db_session, branch_with_items, line, item_seq=i, customer_id=CUSTOMER_ID
        )
        for i, line in enumerate(lines, start=1)
    ]
    buckets = aggregate_tax_buckets(items)

    assert buckets["taxblAmtB"] == Decimal("150.00")
    assert buckets["taxAmtB"] == Decimal("24.00")


async def test_discount_reduces_taxable_amount(db_session, branch_with_items):
    line = ClientSalesInvoiceLine(
        sku="BREAD",
        description="Bread",
        quantity=Decimal("1"),
        unit_of_measure="BA",
        unit_price=Decimal("100.00"),
        discount_pct=Decimal("10"),
        tax_category="STANDARD16",
    )
    item = await map_sales_line(
        db_session, branch_with_items, line, item_seq=1, customer_id=CUSTOMER_ID
    )

    assert item.splyAmt == Decimal("100.00")
    assert item.dcAmt == Decimal("10.00")
    assert item.taxblAmt == Decimal("90.00")
    assert item.totTaxAmt == Decimal("14.40")
    assert item.totAmt == Decimal("104.40")


async def test_unregistered_sku_raises(db_session, branch_with_items):
    line = ClientSalesInvoiceLine(
        sku="NOT-REGISTERED",
        description="Ghost",
        quantity=Decimal("1"),
        unit_of_measure="BA",
        unit_price=Decimal("1.00"),
        tax_category="STANDARD16",
    )
    with pytest.raises(CodeLookupError):
        await map_sales_line(
            db_session, branch_with_items, line, item_seq=1, customer_id=CUSTOMER_ID
        )


async def test_map_client_sales_invoice_to_kra_full_flow(db_session, branch_with_items):
    payload = ClientSalesInvoice(
        branch_code="NAIROBI-HQ",
        external_invoice_ref="SAP-BILLDOC-001",
        customer_pin="P123456789A",
        customer_name="John Customer",
        payment_method="CASH",
        invoice_date=date(2020, 2, 26),
        lines=[
            ClientSalesInvoiceLine(
                sku="BREAD",
                description="Bread",
                quantity=Decimal("2"),
                unit_of_measure="BA",
                unit_price=Decimal("100.00"),
                tax_category="STANDARD16",
            )
        ],
    )

    req = await map_client_sales_invoice_to_kra(
        db_session,
        branch_with_items,
        payload,
        regr_id="admin",
        regr_nm="Administrator",
        customer_id=CUSTOMER_ID,
    )

    assert req.trdInvcNo == "SAP-BILLDOC-001"
    assert req.invcNo == "1"
    assert req.salesDt == "20200226"
    assert req.pmtTyCd == "01"
    assert req.totItemCnt == 1
    assert req.totTaxblAmt == Decimal("200.00")
    assert req.totTaxAmt == Decimal("32.00")
    assert req.totAmt == Decimal("232.00")
    assert req.itemList[0].itemCd == "KE2NTBA0000001"
