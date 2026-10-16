"""Tests for VSCUSalesService.save_sale (app/services/vscu_sales_service.py) — the
highest-stakes logic in the VSCU integration: idempotency, the 836 sequence-error recovery
path, and the 924 duplicate-submission reconciliation path (see the module's own docstring
for why each is handled the way it is).
"""

from datetime import date
from decimal import Decimal

import httpx
import pytest
import respx
from httpx import Response
from sqlalchemy import select

from app.core.config import Settings
from app.db.models import (
    Branch,
    Client,
    Customer,
    IdempotencyKey,
    Item,
    KraCodeList,
    SalesInvoice,
    VscuDeviceState,
)
from app.integrations.vscu.client import VSCUClient
from app.integrations.vscu.exceptions import (
    VSCUDuplicateSubmissionError,
    VSCUTransportError,
    VSCUValidationError,
)
from app.schemas.client.credit_note_in import ClientCreditNoteInvoice
from app.schemas.client.sales_in import ClientSalesInvoice, ClientSalesInvoiceLine
from app.services.client_auth import hash_client_secret
from app.services.vscu_sales_service import (
    STATUS_FISCALIZED,
    STATUS_SIGNED_PENDING_TRANSMISSION,
    VSCUSalesService,
    VSCUSequenceErrorNeedsResync,
)

VSCU_URL = "http://vscu-branch.test:8088"


def sales_envelope(
    result_cd="000", result_msg="The request has been processed successfully", **data
):
    return {
        "resultCd": result_cd,
        "resultMsg": result_msg,
        "resultDt": "20260901120000",
        "data": data
        or {
            "rcptNo": 1,
            "totRcptNo": 1,
            "intrlData": "1" * 26,
            "rcptSign": "2" * 16,
            "vsdcRcptPbctDate": "20260901120000",
            "sdcId": "SDC001",
            "mrcNo": "MRC001",
        },
    }


@pytest.fixture
def vscu_client(db_session) -> VSCUClient:
    return VSCUClient(db=db_session, settings=Settings(vscu_enabled=True))


@pytest.fixture
async def branch(db_session) -> Branch:
    b = Branch(
        branch_code="VSCU-SALES",
        tin="A123456789Z",
        bhf_id="00",
        cmc_key="unused",
        device_type="VSCU",
    )
    db_session.add(b)
    await db_session.flush()
    db_session.add(VscuDeviceState(branch_id=b.id, base_url=VSCU_URL, dvc_srl_no="SN-1"))
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


@pytest.fixture
async def client(db_session, branch: Branch) -> Client:
    customer = Customer(name="SAP-TEST", allowed_branch_ids=[branch.id])
    db_session.add(customer)
    await db_session.flush()
    c = Client(
        name="SAP-TEST",
        client_secret_hash=hash_client_secret("irrelevant"),
        customer_id=customer.id,
    )
    db_session.add(c)
    await db_session.flush()
    return c


def _payload(ref: str = "SAP-1") -> ClientSalesInvoice:
    return ClientSalesInvoice(
        branch_code="VSCU-SALES",
        external_invoice_ref=ref,
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


async def test_successful_sale_persists_fiscalized(
    db_session, branch: Branch, vscu_client: VSCUClient, client: Client
):
    service = VSCUSalesService(db_session, vscu_client)
    with respx.mock:
        respx.post(f"{VSCU_URL}/trnsSales/saveSales").mock(
            return_value=Response(200, json=sales_envelope())
        )
        invoice, response = await service.save_sale(client.id, branch, _payload())

    assert invoice.status == STATUS_FISCALIZED
    assert invoice.kra_rcpt_sign == "2" * 16
    assert response["status"] == STATUS_FISCALIZED


async def test_signed_but_not_transmitted_persists_distinct_status(
    db_session, branch: Branch, vscu_client: VSCUClient, client: Client
):
    """Verified against decompiled TrnsSalesExcute.saveTrnsSales: offline/queued sales get
    resultCd rewritten to "000" with resultMsg="INVOICE IS SIGNED BUT NOT TRANSMITTED" before
    ever reaching us — this must be persisted as genuinely signed, not conflated with a fully
    transmitted receipt.
    """
    service = VSCUSalesService(db_session, vscu_client)
    with respx.mock:
        respx.post(f"{VSCU_URL}/trnsSales/saveSales").mock(
            return_value=Response(
                200, json=sales_envelope(result_msg="INVOICE IS SIGNED BUT NOT TRANSMITTED")
            )
        )
        invoice, response = await service.save_sale(client.id, branch, _payload())

    assert invoice.status == STATUS_SIGNED_PENDING_TRANSMISSION
    assert response["status"] == STATUS_SIGNED_PENDING_TRANSMISSION


async def test_resubmitting_same_external_ref_does_not_call_vscu_again(
    db_session, branch: Branch, vscu_client: VSCUClient, client: Client
):
    service = VSCUSalesService(db_session, vscu_client)
    with respx.mock:
        route = respx.post(f"{VSCU_URL}/trnsSales/saveSales").mock(
            return_value=Response(200, json=sales_envelope())
        )
        await service.save_sale(client.id, branch, _payload("SAP-DUP"))
        await service.save_sale(client.id, branch, _payload("SAP-DUP"))

    assert route.call_count == 1


async def test_sequence_error_releases_claim_and_raises_needs_resync(
    db_session, branch: Branch, vscu_client: VSCUClient, client: Client
):
    service = VSCUSalesService(db_session, vscu_client)
    with respx.mock:
        respx.post(f"{VSCU_URL}/trnsSales/saveSales").mock(
            return_value=Response(
                200,
                json=sales_envelope(
                    result_cd="836",
                    result_msg=(
                        "Your Sequences have been altered, Connect to KRA API to get Sequences."
                    ),
                    **{},
                ),
            )
        )
        with pytest.raises(VSCUSequenceErrorNeedsResync):
            await service.save_sale(client.id, branch, _payload("SAP-SEQ-ERR"))

    # Claim released, not stuck — same external_ref must be retryable.
    claim = await db_session.scalar(
        select(IdempotencyKey).where(
            IdempotencyKey.client_id == client.id,
            IdempotencyKey.external_ref == "SAP-SEQ-ERR",
            IdempotencyKey.endpoint == "/v1/sales-invoices",
        )
    )
    assert claim is None
    invoices = (
        (await db_session.execute(select(SalesInvoice).where(SalesInvoice.branch_id == branch.id)))
        .scalars()
        .all()
    )
    assert invoices == []


async def test_duplicate_submission_reconciles_via_invoice_details(
    db_session, branch: Branch, vscu_client: VSCUClient, client: Client
):
    """924 ("INVOICE NUMBER IS ALREADY PROCESSED") is treated as the ambiguous-result case it
    is (spec §26) — reconciled via selectInvoiceDetails rather than surfaced as a hard failure.
    """
    service = VSCUSalesService(db_session, vscu_client)
    with respx.mock:
        respx.post(f"{VSCU_URL}/trnsSales/saveSales").mock(
            return_value=Response(
                200,
                json=sales_envelope(
                    result_cd="924", result_msg="INVOICE NUMBER IS ALREADY PROCESSED", **{}
                ),
            )
        )
        respx.post(f"{VSCU_URL}/trnsSales/selectInvoiceDetails").mock(
            return_value=Response(
                200,
                json={
                    "resultCd": "000",
                    "resultMsg": "ok",
                    "resultDt": "20260901120000",
                    "data": {
                        "salesList": [
                            {
                                "invcNo": 1,
                                "itemList": [],
                                "receipt": {
                                    "curRcptNo": "1",
                                    "totRcptNo": "1",
                                    "intrlData": "1" * 26,
                                    "rcptSign": "2" * 16,
                                    "sdcDateTime": "20260901120000",
                                },
                            }
                        ]
                    },
                },
            )
        )
        invoice, response = await service.save_sale(client.id, branch, _payload("SAP-DUP-RECON"))

    assert invoice.status == STATUS_FISCALIZED
    assert invoice.kra_rcpt_sign == "2" * 16
    assert response["rcptSign"] == "2" * 16


async def test_duplicate_submission_unresolvable_reraises_and_releases_claim(
    db_session, branch: Branch, vscu_client: VSCUClient, client: Client
):
    """If reconciliation can't confirm the duplicate either, the original error must surface
    (not be silently swallowed), and the claim must be released so this can be retried.
    """
    service = VSCUSalesService(db_session, vscu_client)
    with respx.mock:
        respx.post(f"{VSCU_URL}/trnsSales/saveSales").mock(
            return_value=Response(
                200,
                json=sales_envelope(
                    result_cd="924", result_msg="INVOICE NUMBER IS ALREADY PROCESSED", **{}
                ),
            )
        )
        respx.post(f"{VSCU_URL}/trnsSales/selectInvoiceDetails").mock(
            return_value=Response(
                200,
                json={
                    "resultCd": "000",
                    "resultMsg": "ok",
                    "resultDt": "20260901120000",
                    "data": {"salesList": []},
                },
            )
        )
        with pytest.raises(VSCUDuplicateSubmissionError):
            await service.save_sale(client.id, branch, _payload("SAP-DUP-UNRESOLVED"))

    claim = await db_session.scalar(
        select(IdempotencyKey).where(
            IdempotencyKey.client_id == client.id,
            IdempotencyKey.external_ref == "SAP-DUP-UNRESOLVED",
            IdempotencyKey.endpoint == "/v1/sales-invoices",
        )
    )
    assert claim is None


async def test_local_validation_error_releases_claim(
    db_session, branch: Branch, vscu_client: VSCUClient, client: Client
):
    """834 (bad salesTyCd/rcptTyCd combo) is a genuine rejection — not ambiguous — so the
    claim is released for a corrected resubmission under the same external_ref.
    """
    service = VSCUSalesService(db_session, vscu_client)
    with respx.mock:
        respx.post(f"{VSCU_URL}/trnsSales/saveSales").mock(
            return_value=Response(
                200, json=sales_envelope(result_cd="834", result_msg="bad combo", **{})
            )
        )
        with pytest.raises(VSCUValidationError):
            await service.save_sale(client.id, branch, _payload("SAP-834"))

    claim = await db_session.scalar(
        select(IdempotencyKey).where(
            IdempotencyKey.client_id == client.id,
            IdempotencyKey.external_ref == "SAP-834",
            IdempotencyKey.endpoint == "/v1/sales-invoices",
        )
    )
    assert claim is None


async def test_transport_failure_reconciles_via_invoice_details(
    db_session, branch: Branch, vscu_client: VSCUClient, client: Client
):
    """The other ambiguous-result case (spec §26): our own connection to the local VSCU app
    fails/times out before any HTTP response — we don't know if it was received. Reconciled
    the same way as 924, via selectInvoiceDetails.
    """
    service = VSCUSalesService(db_session, vscu_client)
    with respx.mock:
        respx.post(f"{VSCU_URL}/trnsSales/saveSales").mock(
            side_effect=httpx.ConnectError("refused")
        )
        respx.post(f"{VSCU_URL}/trnsSales/selectInvoiceDetails").mock(
            return_value=Response(
                200,
                json={
                    "resultCd": "000",
                    "resultMsg": "ok",
                    "resultDt": "20260901120000",
                    "data": {
                        "salesList": [
                            {
                                "invcNo": 1,
                                "itemList": [],
                                "receipt": {
                                    "curRcptNo": "1",
                                    "totRcptNo": "1",
                                    "intrlData": "1" * 26,
                                    "rcptSign": "2" * 16,
                                    "sdcDateTime": "20260901120000",
                                },
                            }
                        ]
                    },
                },
            )
        )
        invoice, response = await service.save_sale(
            client.id, branch, _payload("SAP-TIMEOUT-RECON")
        )

    assert invoice.status == STATUS_FISCALIZED
    assert invoice.kra_rcpt_sign == "2" * 16
    assert response["rcptSign"] == "2" * 16


async def test_transport_failure_unresolvable_reraises_and_releases_claim(
    db_session, branch: Branch, vscu_client: VSCUClient, client: Client
):
    service = VSCUSalesService(db_session, vscu_client)
    with respx.mock:
        respx.post(f"{VSCU_URL}/trnsSales/saveSales").mock(
            side_effect=httpx.ConnectError("refused")
        )
        respx.post(f"{VSCU_URL}/trnsSales/selectInvoiceDetails").mock(
            return_value=Response(
                200,
                json={
                    "resultCd": "000",
                    "resultMsg": "ok",
                    "resultDt": "20260901120000",
                    "data": {"salesList": []},
                },
            )
        )
        with pytest.raises(VSCUTransportError):
            await service.save_sale(client.id, branch, _payload("SAP-TIMEOUT-UNRESOLVED"))

    claim = await db_session.scalar(
        select(IdempotencyKey).where(
            IdempotencyKey.client_id == client.id,
            IdempotencyKey.external_ref == "SAP-TIMEOUT-UNRESOLVED",
            IdempotencyKey.endpoint == "/v1/sales-invoices",
        )
    )
    assert claim is None


async def test_save_credit_note_persists_with_correct_kind_and_original_ref(
    db_session, branch: Branch, vscu_client: VSCUClient, client: Client
):
    db_session.add(
        SalesInvoice(
            branch_id=branch.id,
            external_ref="ORIGINAL-1",
            transaction_kind="SALE",
            kra_invc_no=5,
            status="FISCALIZED",
        )
    )
    await db_session.flush()
    db_session.add(KraCodeList(cd_cls="32", cd="06", cd_nm="Refund"))
    await db_session.flush()

    service = VSCUSalesService(db_session, vscu_client)
    payload = ClientCreditNoteInvoice(
        branch_code="VSCU-SALES",
        external_invoice_ref="CN-1",
        original_invoice_ref="ORIGINAL-1",
        reason="Refund",
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
    with respx.mock:
        respx.post(f"{VSCU_URL}/trnsSales/saveSales").mock(
            return_value=Response(200, json=sales_envelope())
        )
        invoice, _response = await service.save_credit_note(client.id, branch, payload)

    assert invoice.transaction_kind == "CREDIT_NOTE"
    assert invoice.original_ref == "ORIGINAL-1"
    assert invoice.status == STATUS_FISCALIZED


async def test_sale_and_credit_note_can_share_the_same_external_ref(
    db_session, branch: Branch, vscu_client: VSCUClient, client: Client
):
    """Regression test: SalesInvoice's uniqueness includes transaction_kind precisely so a
    normal sale and an unrelated credit note don't collide just because a client reused the
    same ref string — they're independent idempotency namespaces.
    """
    db_session.add(
        SalesInvoice(
            branch_id=branch.id,
            external_ref="ORIGINAL-1",
            transaction_kind="SALE",
            kra_invc_no=5,
            status="FISCALIZED",
        )
    )
    await db_session.flush()
    db_session.add(KraCodeList(cd_cls="32", cd="06", cd_nm="Refund"))
    await db_session.flush()

    service = VSCUSalesService(db_session, vscu_client)

    with respx.mock:
        respx.post(f"{VSCU_URL}/trnsSales/saveSales").mock(
            return_value=Response(200, json=sales_envelope())
        )
        # Reuse the SAME string "SHARED-REF" as both a new sale's external ref and a credit
        # note's external ref — must not collide.
        sale_invoice, _ = await service.save_sale(client.id, branch, _payload("SHARED-REF"))
        credit_note_payload = ClientCreditNoteInvoice(
            branch_code="VSCU-SALES",
            external_invoice_ref="SHARED-REF",
            original_invoice_ref="ORIGINAL-1",
            reason="Refund",
            lines=[
                ClientSalesInvoiceLine(
                    sku="BREAD",
                    description="Bread",
                    quantity=Decimal("1"),
                    unit_of_measure="BA",
                    unit_price=Decimal("100.00"),
                    tax_category="STANDARD16",
                )
            ],
        )
        credit_note_invoice, _ = await service.save_credit_note(
            client.id, branch, credit_note_payload
        )

    assert sale_invoice.transaction_kind == "SALE"
    assert credit_note_invoice.transaction_kind == "CREDIT_NOTE"
    assert sale_invoice.id != credit_note_invoice.id
