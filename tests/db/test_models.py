"""Round-trip create/read tests for the core data model (spec §17)."""

import uuid
from decimal import Decimal

from sqlalchemy import select

from app.db.models import (
    Branch,
    Client,
    IdempotencyKey,
    Item,
    KraCallLog,
    KraCodeList,
    KraSyncState,
    Outbox,
    SalesInvoice,
)


async def test_branch_round_trip(db_session):
    branch = Branch(
        branch_code="NAIROBI-HQ",
        tin="A123456789Z",
        bhf_id="00",
        cmc_key="fake-cmc-key",
    )
    db_session.add(branch)
    await db_session.flush()

    fetched = await db_session.get(Branch, branch.id)
    assert fetched is not None
    assert fetched.branch_code == "NAIROBI-HQ"
    assert fetched.tin == "A123456789Z"
    assert fetched.status == "ACTIVE"
    assert fetched.created_at is not None


async def test_client_round_trip(db_session):
    branch_id = uuid.uuid4()
    client = Client(
        name="SAP-PROD",
        api_key_hash="hashed-key",
        allowed_branch_ids=[branch_id],
    )
    db_session.add(client)
    await db_session.flush()

    fetched = await db_session.get(Client, client.id)
    assert fetched is not None
    assert fetched.allowed_branch_ids == [branch_id]


async def test_item_round_trip_and_branch_relationship(db_session):
    branch = Branch(branch_code="MOMBASA-01", tin="A123456789Z", bhf_id="01", cmc_key="key")
    db_session.add(branch)
    await db_session.flush()

    item = Item(
        branch_id=branch.id,
        sku="SKU-001",
        item_cd="KE2NTBA0000012",
        item_cls_cd="5020150101",
        item_nm="Widget",
        tax_ty_cd="B",
        dft_prc=Decimal("199.99"),
        pkg_unit_cd="NT",
        qty_unit_cd="BA",
    )
    db_session.add(item)
    await db_session.flush()

    fetched = await db_session.get(Item, item.id)
    assert fetched is not None
    assert fetched.dft_prc == Decimal("199.99")
    assert fetched.branch.branch_code == "MOMBASA-01"


async def test_kra_code_list_round_trip(db_session):
    row = KraCodeList(cd_cls="04", cd="B", cd_nm="16%", srt_ord=2, use_yn="Y")
    db_session.add(row)
    await db_session.flush()

    fetched = await db_session.get(KraCodeList, {"cd_cls": "04", "cd": "B"})
    assert fetched is not None
    assert fetched.cd_nm == "16%"


async def test_kra_sync_state_round_trip(db_session):
    state = KraSyncState(
        tin="A123456789Z", bhf_id="00", resource="codes", last_req_dt="20200101000000"
    )
    db_session.add(state)
    await db_session.flush()

    fetched = await db_session.get(
        KraSyncState, {"tin": "A123456789Z", "bhf_id": "00", "resource": "codes"}
    )
    assert fetched is not None
    assert fetched.last_req_dt == "20200101000000"


async def test_idempotency_key_round_trip(db_session):
    client = Client(name="POS-VENDOR-X", api_key_hash="hash", allowed_branch_ids=[])
    db_session.add(client)
    await db_session.flush()

    key = IdempotencyKey(
        client_id=client.id,
        external_ref="SAP-BILLDOC-123",
        endpoint="/v1/sales-invoices",
        kra_response={"curRcptNo": 42, "rcptSign": "abc"},
    )
    db_session.add(key)
    await db_session.flush()

    result = await db_session.execute(
        select(IdempotencyKey).where(
            IdempotencyKey.client_id == client.id,
            IdempotencyKey.external_ref == "SAP-BILLDOC-123",
            IdempotencyKey.endpoint == "/v1/sales-invoices",
        )
    )
    fetched = result.scalar_one()
    assert fetched.kra_response == {"curRcptNo": 42, "rcptSign": "abc"}


async def test_outbox_round_trip(db_session):
    row = Outbox(endpoint="/saveItem", payload={"itemCd": "KE2NTBA0000012"})
    db_session.add(row)
    await db_session.flush()

    fetched = await db_session.get(Outbox, row.id)
    assert fetched is not None
    assert fetched.status == "PENDING"
    assert fetched.attempts == 0
    assert fetched.payload == {"itemCd": "KE2NTBA0000012"}


async def test_kra_call_log_round_trip(db_session):
    row = KraCallLog(
        endpoint="/saveTrnsSalesOsdc",
        request_hash="deadbeef",
        result_cd="000",
        result_msg="It is succeeded",
        result_dt="20200226143124",
    )
    db_session.add(row)
    await db_session.flush()

    fetched = await db_session.get(KraCallLog, row.id)
    assert fetched is not None
    assert fetched.result_cd == "000"
    assert fetched.retry_count == 0


async def test_sales_invoice_round_trip(db_session):
    branch = Branch(branch_code="KISUMU-01", tin="A123456789Z", bhf_id="02", cmc_key="key")
    db_session.add(branch)
    await db_session.flush()

    invoice = SalesInvoice(
        branch_id=branch.id,
        external_ref="SAP-BILLDOC-999",
        kra_cur_rcpt_no=1,
        kra_tot_rcpt_no=1,
        kra_rcpt_sign="A1B2C3D4E5F6G7H8",
        kra_intrl_data="1" * 26,
        kra_sdc_datetime="20200226143124",
    )
    db_session.add(invoice)
    await db_session.flush()

    fetched = await db_session.get(SalesInvoice, invoice.id)
    assert fetched is not None
    assert fetched.status == "PENDING"
    assert fetched.kra_rcpt_sign == "A1B2C3D4E5F6G7H8"
