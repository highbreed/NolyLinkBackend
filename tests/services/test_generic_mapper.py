"""Tests for the client -> KRA item mapper (spec §9.2)."""

import pytest

from app.db.models import Branch, KraCodeList
from app.schemas.client.item_in import ClientItemIn
from app.services.mapping.generic_mapper import (
    CodeLookupError,
    map_client_item_to_kra,
    map_product_type,
    map_tax_category,
)


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
    db_session.add_all(
        [
            KraCodeList(cd_cls="17", cd="NT", cd_nm="Net"),
            KraCodeList(cd_cls="10", cd="BA", cd_nm="Bag"),
        ]
    )
    await db_session.flush()

    payload = ClientItemIn(
        branch_code="NAIROBI-HQ",
        sku="SKU-001",
        name="Bread 400g",
        item_classification_cd="5020150101",
        unit_of_measure="Bag",
        packaging_unit="Net",
        product_type="FINISHED_PRODUCT",
        tax_category="STANDARD16",
        unit_price="100.00",
    )

    req, item_cd = await map_client_item_to_kra(
        db_session, branch, payload, regr_id="admin", regr_nm="Administrator"
    )

    assert item_cd == "KE2NTBA0000001"
    assert req.itemCd == item_cd
    assert req.taxTyCd == "B"
    assert req.itemTyCd == "2"
    assert req.pkgUnitCd == "NT"
    assert req.qtyUnitCd == "BA"


async def test_map_client_item_to_kra_reuses_existing_item_cd(db_session):
    branch = Branch(branch_code="MOMBASA-01", tin="A123456789Z", bhf_id="01", cmc_key="c" * 10)
    db_session.add(branch)
    db_session.add_all(
        [
            KraCodeList(cd_cls="17", cd="NT"),
            KraCodeList(cd_cls="10", cd="BA"),
        ]
    )
    await db_session.flush()

    payload = ClientItemIn(
        branch_code="MOMBASA-01",
        sku="SKU-002",
        name="Bread 400g",
        item_classification_cd="5020150101",
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
        existing_item_cd="KE2NTBA0000099",
    )

    assert item_cd == "KE2NTBA0000099"


async def test_map_client_item_to_kra_raises_on_unmapped_code(db_session):
    branch = Branch(branch_code="KISUMU-01", tin="A123456789Z", bhf_id="00", cmc_key="c" * 10)
    db_session.add(branch)
    await db_session.flush()

    payload = ClientItemIn(
        branch_code="KISUMU-01",
        sku="SKU-003",
        name="Bread 400g",
        item_classification_cd="5020150101",
        unit_of_measure="NOT-CACHED",
        packaging_unit="NT",
        product_type="FINISHED_PRODUCT",
        tax_category="STANDARD16",
        unit_price="100.00",
    )

    with pytest.raises(CodeLookupError):
        await map_client_item_to_kra(db_session, branch, payload, "admin", "Administrator")
