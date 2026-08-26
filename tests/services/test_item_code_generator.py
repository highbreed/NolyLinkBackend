"""Tests for the itemCd generator (spec §8) — including the spec's own worked example."""

from app.services.item_code_generator import generate_item_cd

COMPOSITE = {
    "tin": "A123456789Z",
    "bhf_id": "00",
    "orgn_nat_cd": "KE",
    "item_ty_cd": "2",
    "pkg_unit_cd": "NT",
    "qty_unit_cd": "BA",
}


async def test_worked_example_ke2ntba0000012(db_session):
    codes = [await generate_item_cd(db_session, **COMPOSITE) for _ in range(12)]

    assert codes[0] == "KE2NTBA0000001"
    assert codes[-1] == "KE2NTBA0000012"


async def test_sequence_is_scoped_per_composite_key(db_session):
    first_a = await generate_item_cd(db_session, **COMPOSITE)
    first_b = await generate_item_cd(db_session, **{**COMPOSITE, "item_ty_cd": "1"})

    assert first_a == "KE2NTBA0000001"
    assert first_b == "KE1NTBA0000001"


async def test_sequence_survives_across_calls_with_separate_flushes(db_session):
    await generate_item_cd(db_session, **COMPOSITE)
    await db_session.flush()
    second = await generate_item_cd(db_session, **COMPOSITE)

    assert second == "KE2NTBA0000002"
