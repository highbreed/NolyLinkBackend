"""End-to-end sales invoice test against the real KRA sandbox (spec §19: "contract tests
against KRA sandbox... for every adapter method before promoting to production").

Skipped by default — requires real sandbox credentials, which haven't been provided to this
build (see the build prompt's "Ask before assuming" #2: "do not fabricate and test against a
fake endpoint and call it done"). To run it for real:

    export KRA_SANDBOX_INTEGRATION_TESTS=1
    export KRA_SANDBOX_TEST_TIN=...
    export KRA_SANDBOX_TEST_BHF_ID=...
    export KRA_SANDBOX_TEST_CMC_KEY=...   # from a prior /v1/devices/initialize sandbox call
    ./venv/bin/pytest tests/integration/test_sales_sandbox.py -v

Recorded-cassette (VCR-style) fallback: none is checked in. A cassette needs a genuine
recorded request/response exchange with KRA sandbox, which — like the golden fixtures in
Phase 2 and the seed data in Phase 5 — isn't something to fabricate; a fake cassette would be
worse than no cassette; it'd assert a shape KRA's real API might not actually return. The
respx-based tests in tests/api/test_sales_endpoint.py cover the same code path against a
synthetic KRA response and are what actually runs in CI.
"""

import os
from datetime import date
from decimal import Decimal

import pytest

from app.db.models import Branch, Item
from app.schemas.client.item_in import ClientItemIn
from app.schemas.client.sales_in import ClientSalesInvoice, ClientSalesInvoiceLine
from app.services.kra_adapter import KraAdapter
from app.services.mapping.generic_mapper import (
    map_client_item_to_kra,
    map_client_sales_invoice_to_kra,
)

REQUIRED_ENV_VARS = ("KRA_SANDBOX_TEST_TIN", "KRA_SANDBOX_TEST_BHF_ID", "KRA_SANDBOX_TEST_CMC_KEY")

pytestmark = pytest.mark.skipif(
    os.environ.get("KRA_SANDBOX_INTEGRATION_TESTS") != "1"
    or not all(os.environ.get(v) for v in REQUIRED_ENV_VARS),
    reason=(
        "Real KRA sandbox credentials not configured — set KRA_SANDBOX_INTEGRATION_TESTS=1 "
        f"plus {', '.join(REQUIRED_ENV_VARS)} to run this against the live sandbox."
    ),
)


@pytest.fixture
async def sandbox_branch(db_session) -> Branch:
    branch = Branch(
        branch_code="SANDBOX-INTEGRATION-TEST",
        tin=os.environ["KRA_SANDBOX_TEST_TIN"],
        bhf_id=os.environ["KRA_SANDBOX_TEST_BHF_ID"],
        cmc_key=os.environ["KRA_SANDBOX_TEST_CMC_KEY"],
    )
    db_session.add(branch)
    await db_session.flush()
    return branch


async def test_submit_sales_invoice_against_real_sandbox(db_session, sandbox_branch):
    kra = KraAdapter(db=db_session)
    try:
        # Register a throwaway item first, same as a real client would via POST /v1/items.
        item_payload = ClientItemIn(
            branch_code=sandbox_branch.branch_code,
            sku="SANDBOX-TEST-SKU",
            name="Sandbox Integration Test Item",
            item_classification_cd="5020150101",
            unit_of_measure="U",
            packaging_unit="NT",
            product_type="FINISHED_PRODUCT",
            tax_category="STANDARD16",
            unit_price=Decimal("1.00"),
        )
        item_req, item_cd = await map_client_item_to_kra(
            db_session, sandbox_branch, item_payload, regr_id="admin", regr_nm="Administrator"
        )
        item_res = await kra.save_item(item_req, branch_id=sandbox_branch.id)
        assert item_res.resultCd == "000"

        db_session.add(
            Item(
                branch_id=sandbox_branch.id,
                sku=item_payload.sku,
                item_cd=item_cd,
                item_cls_cd=item_payload.item_classification_cd,
                item_nm=item_payload.name,
                tax_ty_cd="B",
                dft_prc=item_payload.unit_price,
                pkg_unit_cd=item_req.pkgUnitCd,
                qty_unit_cd=item_req.qtyUnitCd,
            )
        )
        await db_session.flush()

        sales_payload = ClientSalesInvoice(
            branch_code=sandbox_branch.branch_code,
            external_invoice_ref=f"SANDBOX-TEST-{item_cd}",
            payment_method="CASH",
            invoice_date=date.today(),
            lines=[
                ClientSalesInvoiceLine(
                    sku=item_payload.sku,
                    description="Sandbox Integration Test Item",
                    quantity=Decimal("1"),
                    unit_of_measure="U",
                    unit_price=Decimal("1.00"),
                    tax_category="STANDARD16",
                )
            ],
        )
        sales_req = await map_client_sales_invoice_to_kra(
            db_session, sandbox_branch, sales_payload, regr_id="admin", regr_nm="Administrator"
        )
        sales_res = await kra.save_sales_transaction(sales_req, branch_id=sandbox_branch.id)

        assert sales_res.resultCd == "000"
        assert sales_res.data is not None
        assert sales_res.data.rcptSign
        assert sales_res.data.curRcptNo > 0
    finally:
        await kra.aclose()
