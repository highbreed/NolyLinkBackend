"""Tests for map_client_sales_invoice_to_vscu (app/services/mapping/vscu_sales_mapper.py):
tax-bucket math, Decimal precision, salesTyCd/rcptTyCd defaults, and the per-line taxAmt field
name (VSCU's own name — distinct from OSCU's totTaxAmt, see the module's own docstring).
"""

from datetime import date
from decimal import Decimal

import pytest

from app.db.models import Branch, Item, KraCodeList
from app.schemas.client.sales_in import ClientSalesInvoice, ClientSalesInvoiceLine
from app.services.mapping.generic_mapper import CodeLookupError
from app.services.mapping.vscu_sales_mapper import map_client_sales_invoice_to_vscu


@pytest.fixture
async def branch(db_session) -> Branch:
    b = Branch(
        branch_code="VSCU-BR", tin="A123456789Z", bhf_id="00", cmc_key="unused", device_type="VSCU"
    )
    db_session.add(b)
    db_session.add(
        Item(
            branch=b,
            sku="BREAD",
            item_cd="KE2NTBA0000001",
            item_cls_cd="5020150101",
            item_nm="Bread",
            tax_ty_cd="B",
            dft_prc=Decimal("100.00"),
            pkg_unit_cd="NT",
            qty_unit_cd="BA",
        )
    )
    db_session.add(KraCodeList(cd_cls="07", cd="01", cd_nm="CASH"))
    await db_session.flush()
    return b


def _payload(**overrides) -> ClientSalesInvoice:
    defaults = dict(
        branch_code="VSCU-BR",
        external_invoice_ref="SAP-1",
        customer_pin="P123456789A",
        customer_name="John Customer",
        payment_method="CASH",
        invoice_date=date(2026, 9, 1),
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
    defaults.update(overrides)
    return ClientSalesInvoice.model_validate(defaults)


async def test_maps_header_defaults(db_session, branch: Branch):
    req = await map_client_sales_invoice_to_vscu(
        db_session, branch, _payload(), regr_id="gateway", regr_nm="Gateway"
    )
    assert req.salesTyCd == "N"
    assert req.rcptTyCd == "S"
    assert req.tin == branch.tin
    assert req.bhfId == branch.bhf_id
    assert req.totItemCnt == 1


async def test_tax_bucket_math_standard_rate(db_session, branch: Branch):
    req = await map_client_sales_invoice_to_vscu(
        db_session, branch, _payload(), regr_id="gateway", regr_nm="Gateway"
    )
    # 2 * 100.00 = 200.00 taxable, 16% -> 32.00 tax, 232.00 total.
    assert req.taxblAmtB == Decimal("200.00")
    assert req.taxAmtB == Decimal("32.00")
    assert req.totTaxblAmt == Decimal("200.00")
    assert req.totTaxAmt == Decimal("32.00")
    assert req.totAmt == Decimal("232.00")
    # Unused buckets are explicitly zeroed, not omitted (all are required header fields).
    assert req.taxblAmtA == Decimal("0.00")
    assert req.taxAmtA == Decimal("0.00")

    item = req.itemList[0]
    assert item.taxAmt == Decimal("32.00")  # VSCU's own field name, not totTaxAmt
    assert item.itemClsCd == "5020150101"


async def test_discount_reduces_taxable_amount(db_session, branch: Branch):
    payload = _payload(
        lines=[
            ClientSalesInvoiceLine(
                sku="BREAD",
                description="Bread",
                quantity=Decimal("2"),
                unit_of_measure="BA",
                unit_price=Decimal("100.00"),
                discount_pct=Decimal("10"),
                tax_category="STANDARD16",
            )
        ]
    )
    req = await map_client_sales_invoice_to_vscu(
        db_session, branch, payload, regr_id="gateway", regr_nm="Gateway"
    )
    # 200.00 supply, 10% discount -> 20.00 off -> 180.00 taxable -> 28.80 tax.
    assert req.itemList[0].dcAmt == Decimal("20.00")
    assert req.taxblAmtB == Decimal("180.00")
    assert req.taxAmtB == Decimal("28.80")


async def test_exempt_line_has_zero_tax(db_session, branch: Branch):
    db_session.add(
        Item(
            branch=branch,
            sku="MILK",
            item_cd="KE2NTBA0000002",
            item_cls_cd="5020150102",
            item_nm="Milk",
            tax_ty_cd="A",
            dft_prc=Decimal("50.00"),
            pkg_unit_cd="NT",
            qty_unit_cd="BA",
        )
    )
    await db_session.flush()
    payload = _payload(
        lines=[
            ClientSalesInvoiceLine(
                sku="MILK",
                description="Milk",
                quantity=Decimal("1"),
                unit_of_measure="BA",
                unit_price=Decimal("50.00"),
                tax_category="EXEMPT",
            )
        ]
    )
    req = await map_client_sales_invoice_to_vscu(
        db_session, branch, payload, regr_id="gateway", regr_nm="Gateway"
    )
    assert req.taxblAmtA == Decimal("50.00")
    assert req.taxAmtA == Decimal("0.00")
    assert req.totAmt == Decimal("50.00")


async def test_unregistered_sku_raises_code_lookup_error(db_session, branch: Branch):
    payload = _payload(
        lines=[
            ClientSalesInvoiceLine(
                sku="UNKNOWN-SKU",
                description="Ghost item",
                quantity=Decimal("1"),
                unit_of_measure="BA",
                unit_price=Decimal("1.00"),
                tax_category="STANDARD16",
            )
        ]
    )
    with pytest.raises(CodeLookupError):
        await map_client_sales_invoice_to_vscu(
            db_session, branch, payload, regr_id="gateway", regr_nm="Gateway"
        )


async def test_invoice_numbers_increment_per_branch(db_session, branch: Branch):
    req1 = await map_client_sales_invoice_to_vscu(
        db_session, branch, _payload(external_invoice_ref="A"), regr_id="g", regr_nm="G"
    )
    req2 = await map_client_sales_invoice_to_vscu(
        db_session, branch, _payload(external_invoice_ref="B"), regr_id="g", regr_nm="G"
    )
    assert req2.invcNo == req1.invcNo + 1
