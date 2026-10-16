"""API tests for GET /v1/admin/reference-data/* — the admin-gated browsing surface over
kra_code_lists/kra_item_classes, used to decide what to assign via
PUT /v1/customers/{id}/code-allowlist/{classification}.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import get_settings
from app.db.models import KraCodeClassification, KraCodeList, KraItemClass
from app.db.session import get_db
from app.main import app


@pytest.fixture
def client(db_session):
    async def _override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    try:
        transport = ASGITransport(app=app)
        yield AsyncClient(transport=transport, base_url="http://test")
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def admin_headers() -> dict[str, str]:
    admin_key = get_settings().admin_api_key
    assert admin_key, "ADMIN_API_KEY must be configured for this test run"
    return {"X-Admin-Api-Key": admin_key}


async def test_list_classifications_summary(client, db_session, admin_headers):
    db_session.add_all(
        [
            KraCodeList(cd_cls="04", cd="A", cd_nm="Exempt"),
            KraCodeList(cd_cls="04", cd="B", cd_nm="16%"),
            KraCodeList(cd_cls="99", cd="X", cd_nm="Unmapped classification"),
            KraCodeClassification(cd_cls="04", cd_cls_nm="Tax Type"),
            # cd_cls "99" has no KraCodeClassification row here, simulating data synced before
            # this field was captured — the summary must still work, cd_cls_nm just comes back
            # null rather than erroring.
        ]
    )
    await db_session.flush()

    async with client as c:
        response = await c.get("/v1/admin/reference-data/codes", headers=admin_headers)

    assert response.status_code == 200
    body = {row["cd_cls"]: row for row in response.json()}
    assert body["04"]["classification"] == "tax-type"
    assert body["04"]["cd_cls_nm"] == "Tax Type"
    assert body["04"]["code_count"] == 2
    # cd_cls 99 has no CLASSIFICATION_SLUGS entry — still listed, keyed by raw cd_cls.
    assert body["99"]["classification"] == "99"
    assert body["99"]["cd_cls_nm"] is None
    assert body["99"]["code_count"] == 1


async def test_list_codes_for_classification(client, db_session, admin_headers):
    db_session.add_all(
        [
            KraCodeList(cd_cls="17", cd="NT", cd_nm="Net"),
            KraCodeList(cd_cls="17", cd="BG", cd_nm="Bag"),
        ]
    )
    await db_session.flush()

    async with client as c:
        response = await c.get(
            "/v1/admin/reference-data/codes/packaging-unit", headers=admin_headers
        )

    assert response.status_code == 200
    codes = {row["cd"] for row in response.json()}
    assert codes == {"NT", "BG"}


async def test_list_codes_filtered_by_q(client, db_session, admin_headers):
    db_session.add_all(
        [
            KraCodeList(cd_cls="17", cd="NT", cd_nm="Net"),
            KraCodeList(cd_cls="17", cd="BG", cd_nm="Bag"),
        ]
    )
    await db_session.flush()

    async with client as c:
        response = await c.get(
            "/v1/admin/reference-data/codes/packaging-unit",
            params={"q": "bag"},
            headers=admin_headers,
        )

    assert response.status_code == 200
    assert [row["cd"] for row in response.json()] == ["BG"]


async def test_list_item_classes_filtered_by_prefix_and_level(client, db_session, admin_headers):
    db_session.add_all(
        [
            KraItemClass(item_cls_cd="50000000", item_cls_nm="Food and Beverage", item_cls_lvl="1"),
            KraItemClass(item_cls_cd="50180000", item_cls_nm="Bread and bakery", item_cls_lvl="2"),
            KraItemClass(
                item_cls_cd="10000000", item_cls_nm="Live Plant Material", item_cls_lvl="1"
            ),
        ]
    )
    await db_session.flush()

    async with client as c:
        response = await c.get(
            "/v1/admin/reference-data/item-classes",
            params={"prefix": "50", "level": "2"},
            headers=admin_headers,
        )

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["item_cls_cd"] == "50180000"


async def test_list_item_classes_filtered_by_q(client, db_session, admin_headers):
    db_session.add_all(
        [
            KraItemClass(item_cls_cd="50180000", item_cls_nm="Bread and bakery products"),
            KraItemClass(item_cls_cd="50200000", item_cls_nm="Beverages"),
        ]
    )
    await db_session.flush()

    async with client as c:
        response = await c.get(
            "/v1/admin/reference-data/item-classes",
            params={"q": "bread"},
            headers=admin_headers,
        )

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["item_cls_cd"] == "50180000"


async def test_reference_data_endpoints_require_admin_key(client):
    async with client as c:
        response = await c.get("/v1/admin/reference-data/codes")
    assert response.status_code == 403
