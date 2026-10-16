"""Tests for the client -> KRA item mapper (spec §9.2)."""

import uuid

import pytest

from app.db.models import Branch, Customer, CustomerCodeAllowlist, KraCodeList, KraItemClass
from app.schemas.client.item_in import ClientItemIn
from app.services.mapping.generic_mapper import (
    CodeLookupError,
    map_client_item_to_kra,
    map_product_type,
    map_tax_category,
)

ITEM_CLS_CD = "5020150101"


def _standard_code_rows() -> list[KraCodeList | KraItemClass]:
    return [
        KraCodeList(cd_cls="17", cd="NT", cd_nm="Net"),
        KraCodeList(cd_cls="10", cd="BA", cd_nm="Bag"),
        KraCodeList(cd_cls="05", cd="KE", cd_nm="Kenya"),
        KraItemClass(item_cls_cd=ITEM_CLS_CD, item_cls_nm="Bread"),
    ]


def test_map_tax_category_matches_spec_table():
    assert map_tax_category("standard16") == "B"
    assert map_tax_category("EXEMPT") == "A"
    assert map_tax_category("zero") == "C"
    assert map_tax_category("NonVAT") == "D"
    assert map_tax_category("8pct") == "E"


def test_map_tax_category_rejects_unknown():
    with pytest.raises(CodeLookupError):
        map_tax_category("BOGUS")


def test_map_product_type_matches_spec_table():
    assert map_product_type("raw_material") == "1"
    assert map_product_type("FINISHED_PRODUCT") == "2"
    assert map_product_type("service") == "3"


async def test_map_client_item_to_kra_generates_item_cd(db_session):
    branch = Branch(branch_code="NAIROBI-HQ", tin="A123456789Z", bhf_id="00", cmc_key="c" * 10)
    db_session.add(branch)
    db_session.add_all(_standard_code_rows())
    await db_session.flush()

    payload = ClientItemIn(
        branch_code="NAIROBI-HQ",
        sku="SKU-001",
        name="Bread 400g",
        item_classification_cd="Bread",
        unit_of_measure="Bag",
        packaging_unit="Net",
        product_type="FINISHED_PRODUCT",
        tax_category="STANDARD16",
        unit_price="100.00",
    )

    req, item_cd = await map_client_item_to_kra(
        db_session,
        branch,
        payload,
        regr_id="admin",
        regr_nm="Administrator",
        customer_id=uuid.uuid4(),
    )

    assert item_cd == "KE2NTBA0000001"
    assert req.itemCd == item_cd
    assert req.taxTyCd == "B"
    assert req.itemTyCd == "2"
    assert req.pkgUnitCd == "NT"
    assert req.qtyUnitCd == "BA"
    assert req.itemClsCd == ITEM_CLS_CD
    assert req.orgnNatCd == "KE"


async def test_map_client_item_to_kra_reuses_existing_item_cd(db_session):
    branch = Branch(branch_code="MOMBASA-01", tin="A123456789Z", bhf_id="01", cmc_key="c" * 10)
    db_session.add(branch)
    db_session.add_all(_standard_code_rows())
    await db_session.flush()

    payload = ClientItemIn(
        branch_code="MOMBASA-01",
        sku="SKU-002",
        name="Bread 400g",
        item_classification_cd=ITEM_CLS_CD,
        unit_of_measure="BA",
        packaging_unit="NT",
        product_type="FINISHED_PRODUCT",
        tax_category="STANDARD16",
        unit_price="105.00",
    )

    _, item_cd = await map_client_item_to_kra(
        db_session,
        branch,
        payload,
        regr_id="admin",
        regr_nm="Administrator",
        customer_id=uuid.uuid4(),
        existing_item_cd="KE2NTBA0000099",
    )

    assert item_cd == "KE2NTBA0000099"


async def test_map_client_item_to_kra_raises_on_unmapped_code(db_session):
    branch = Branch(branch_code="KISUMU-01", tin="A123456789Z", bhf_id="00", cmc_key="c" * 10)
    db_session.add(branch)
    db_session.add_all([KraCodeList(cd_cls="05", cd="KE"), KraItemClass(item_cls_cd=ITEM_CLS_CD)])
    await db_session.flush()

    payload = ClientItemIn(
        branch_code="KISUMU-01",
        sku="SKU-003",
        name="Bread 400g",
        item_classification_cd=ITEM_CLS_CD,
        unit_of_measure="NOT-CACHED",
        packaging_unit="NT",
        product_type="FINISHED_PRODUCT",
        tax_category="STANDARD16",
        unit_price="100.00",
    )

    with pytest.raises(CodeLookupError):
        await map_client_item_to_kra(
            db_session, branch, payload, "admin", "Administrator", customer_id=uuid.uuid4()
        )


async def test_map_client_item_to_kra_raises_on_unmapped_item_classification(db_session):
    branch = Branch(branch_code="ELDORET-01", tin="A123456789Z", bhf_id="00", cmc_key="c" * 10)
    db_session.add(branch)
    db_session.add_all([KraCodeList(cd_cls="17", cd="NT"), KraCodeList(cd_cls="10", cd="BA")])
    await db_session.flush()

    payload = ClientItemIn(
        branch_code="ELDORET-01",
        sku="SKU-004",
        name="Bread 400g",
        item_classification_cd="NOT-CACHED",
        unit_of_measure="BA",
        packaging_unit="NT",
        product_type="FINISHED_PRODUCT",
        tax_category="STANDARD16",
        unit_price="100.00",
    )

    with pytest.raises(CodeLookupError):
        await map_client_item_to_kra(
            db_session, branch, payload, "admin", "Administrator", customer_id=uuid.uuid4()
        )


async def test_map_client_item_to_kra_rejects_code_outside_customer_allowlist(db_session):
    branch = Branch(branch_code="NAKURU-01", tin="A123456789Z", bhf_id="00", cmc_key="c" * 10)
    db_session.add(branch)
    db_session.add_all(_standard_code_rows())
    customer = Customer(name="SAP-RESTRICTED", allowed_branch_ids=[])
    db_session.add(customer)
    await db_session.flush()
    customer_id = customer.id
    db_session.add(
        CustomerCodeAllowlist(customer_id=customer_id, classification="tax-type", code="A")
    )
    await db_session.flush()

    payload = ClientItemIn(
        branch_code="NAKURU-01",
        sku="SKU-005",
        name="Bread 400g",
        item_classification_cd=ITEM_CLS_CD,
        unit_of_measure="BA",
        packaging_unit="NT",
        product_type="FINISHED_PRODUCT",
        tax_category="STANDARD16",  # -> "B", not in the customer's allow-list (only "A")
        unit_price="100.00",
    )

    with pytest.raises(CodeLookupError):
        await map_client_item_to_kra(
            db_session, branch, payload, "admin", "Administrator", customer_id=customer_id
        )


async def test_map_client_item_to_kra_allows_code_inside_customer_allowlist(db_session):
    branch = Branch(branch_code="NAKURU-02", tin="A123456789Z", bhf_id="00", cmc_key="c" * 10)
    db_session.add(branch)
    db_session.add_all(_standard_code_rows())
    customer = Customer(name="SAP-PERMITTED", allowed_branch_ids=[])
    db_session.add(customer)
    await db_session.flush()
    customer_id = customer.id
    db_session.add(
        CustomerCodeAllowlist(customer_id=customer_id, classification="tax-type", code="B")
    )
    await db_session.flush()

    payload = ClientItemIn(
        branch_code="NAKURU-02",
        sku="SKU-006",
        name="Bread 400g",
        item_classification_cd=ITEM_CLS_CD,
        unit_of_measure="BA",
        packaging_unit="NT",
        product_type="FINISHED_PRODUCT",
        tax_category="STANDARD16",  # -> "B", which is on the allow-list
        unit_price="100.00",
    )

    req, _ = await map_client_item_to_kra(
        db_session, branch, payload, "admin", "Administrator", customer_id=customer_id
    )
    assert req.taxTyCd == "B"
