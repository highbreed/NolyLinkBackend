"""Tests for credit-note mapping — both providers, since the business-code semantics are
shared (KRA_OSCU_FastAPI_Integration_Spec.md §7's catalogue, canonical for OSCU and VSCU
alike): rcptTyCd="R" (cdCls 37), Credit Note Reason (cdCls 32) resolved via the code cache,
and orgInvcNo resolved from the original sale's SalesInvoice row — never a mutation of it.
"""

import uuid
from decimal import Decimal

import pytest

from app.db.models import Branch, Item, KraCodeList, SalesInvoice
from app.schemas.client.credit_note_in import ClientCreditNoteInvoice
from app.schemas.client.sales_in import ClientSalesInvoiceLine
from app.services.mapping.generic_mapper import (
    CREDIT_NOTE_RCPT_TY_CD,
    OriginalInvoiceNotFound,
    map_client_credit_note_to_kra,
)
from app.services.mapping.vscu_sales_mapper import (
    CREDIT_NOTE_RCPT_TY_CD as VSCU_CREDIT_NOTE_RCPT_TY_CD,
)
from app.services.mapping.vscu_sales_mapper import (
    DEFAULT_SALES_TY_CD,
    map_client_credit_note_to_vscu,
)


@pytest.fixture
async def branch_with_original_sale(db_session) -> Branch:
    branch = Branch(branch_code="CN-BRANCH", tin="A123456789Z", bhf_id="00", cmc_key="c" * 10)
    db_session.add(branch)
    db_session.add(
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
        )
    )
    db_session.add(KraCodeList(cd_cls="32", cd="06", cd_nm="Refund"))
    await db_session.flush()
    db_session.add(
        SalesInvoice(
            branch_id=branch.id,
            external_ref="ORIGINAL-INV-1",
            transaction_kind="SALE",
            kra_invc_no=42,
            status="FISCALIZED",
        )
    )
    await db_session.flush()
    return branch


def _payload(**overrides) -> ClientCreditNoteInvoice:
    defaults = dict(
        branch_code="CN-BRANCH",
        external_invoice_ref="CN-1",
        original_invoice_ref="ORIGINAL-INV-1",
        reason="Refund",
        customer_pin="P123456789A",
        customer_name="John Customer",
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
    return ClientCreditNoteInvoice.model_validate(defaults)


# No CustomerCodeAllowlist rows are seeded in this file, so any customer_id is unrestricted.
CUSTOMER_ID = uuid.uuid4()


async def test_oscu_credit_note_references_original_invoice(
    db_session, branch_with_original_sale: Branch
):
    req = await map_client_credit_note_to_kra(
        db_session,
        branch_with_original_sale,
        _payload(),
        regr_id="gateway",
        regr_nm="Gateway",
        customer_id=CUSTOMER_ID,
    )
    assert req.orgInvcNo == "42"
    assert req.rcptTyCd == CREDIT_NOTE_RCPT_TY_CD == "R"
    assert req.rfdRsnCd == "06"
    assert req.taxblAmtB == Decimal("200.00")
    assert req.taxAmtB == Decimal("32.00")
    # A new invcNo of its own, not reusing the original's.
    assert req.invcNo != "42"


async def test_oscu_credit_note_unknown_original_raises(
    db_session, branch_with_original_sale: Branch
):
    with pytest.raises(OriginalInvoiceNotFound):
        await map_client_credit_note_to_kra(
            db_session,
            branch_with_original_sale,
            _payload(original_invoice_ref="DOES-NOT-EXIST"),
            regr_id="gateway",
            regr_nm="Gateway",
            customer_id=CUSTOMER_ID,
        )


async def test_oscu_credit_note_cannot_reference_another_credit_note(
    db_session, branch_with_original_sale: Branch
):
    """A credit note's original_invoice_ref must resolve to a SALE, not another credit note —
    guards against transaction_kind ambiguity now that both share the SalesInvoice table.
    """
    db_session.add(
        SalesInvoice(
            branch_id=branch_with_original_sale.id,
            external_ref="SOME-OTHER-CN",
            transaction_kind="CREDIT_NOTE",
            kra_invc_no=99,
            status="FISCALIZED",
        )
    )
    await db_session.flush()
    with pytest.raises(OriginalInvoiceNotFound):
        await map_client_credit_note_to_kra(
            db_session,
            branch_with_original_sale,
            _payload(original_invoice_ref="SOME-OTHER-CN"),
            regr_id="gateway",
            regr_nm="Gateway",
            customer_id=CUSTOMER_ID,
        )


async def test_vscu_credit_note_references_original_invoice(
    db_session, branch_with_original_sale: Branch
):
    req = await map_client_credit_note_to_vscu(
        db_session, branch_with_original_sale, _payload(), regr_id="gateway", regr_nm="Gateway"
    )
    assert req.orgInvcNo == 42
    assert req.salesTyCd == DEFAULT_SALES_TY_CD == "N"
    assert req.rcptTyCd == VSCU_CREDIT_NOTE_RCPT_TY_CD == "R"
    assert req.rfdRsnCd == "06"
    assert req.taxblAmtB == Decimal("200.00")
    assert req.taxAmtB == Decimal("32.00")
    assert req.invcNo != 42


async def test_vscu_credit_note_unknown_original_raises(
    db_session, branch_with_original_sale: Branch
):
    with pytest.raises(OriginalInvoiceNotFound):
        await map_client_credit_note_to_vscu(
            db_session,
            branch_with_original_sale,
            _payload(original_invoice_ref="DOES-NOT-EXIST"),
            regr_id="gateway",
            regr_nm="Gateway",
        )
