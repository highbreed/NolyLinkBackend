"""API tests for GET /v1/item-classes — served from the KraItemClass cache only, the
client-facing counterpart to tests/api/test_codes_endpoint.py.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.db.models import Client, Customer, CustomerCodeAllowlist, KraItemClass
from app.db.session import get_db
from app.main import app
from app.services.client_auth import hash_client_secret
from tests.auth_helpers import bearer_header


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
async def auth_headers(db_session) -> dict[str, str]:
    customer = Customer(name="SAP-TEST", allowed_branch_ids=[])
    db_session.add(customer)
    await db_session.flush()
    registered_client = Client(
        name="SAP-TEST",
        client_secret_hash=hash_client_secret("irrelevant"),
        customer_id=customer.id,
    )
    db_session.add(registered_client)
    await db_session.flush()
    return bearer_header(registered_client)


async def test_get_item_classes(client, db_session, auth_headers):
    db_session.add(
        KraItemClass(
            item_cls_cd="5020150101",
            item_cls_nm="Bread",
            item_cls_lvl="3",
            tax_ty_cd="B",
            mjr_tg_yn="N",
            use_yn="Y",
        )
    )
    await db_session.flush()

    async with client as c:
        response = await c.get("/v1/item-classes", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["item_cls_cd"] == "5020150101"
    assert body[0]["item_cls_nm"] == "Bread"


async def test_get_item_classes_empty_cache_returns_empty_list(client, auth_headers):
    async with client as c:
        response = await c.get("/v1/item-classes", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == []


async def test_get_item_classes_without_token_is_401(client):
    async with client as c:
        response = await c.get("/v1/item-classes")

    assert response.status_code == 401


async def test_get_item_classes_filtered_by_customer_allowlist(client, db_session):
    db_session.add_all(
        [
            KraItemClass(item_cls_cd="5020150101", item_cls_nm="Bread"),
            KraItemClass(item_cls_cd="5020150102", item_cls_nm="Cake"),
        ]
    )
    customer = Customer(name="SAP-RESTRICTED", allowed_branch_ids=[])
    db_session.add(customer)
    await db_session.flush()
    db_session.add(
        CustomerCodeAllowlist(
            customer_id=customer.id, classification="item-classification", code="5020150101"
        )
    )
    restricted_client = Client(
        name="SAP-RESTRICTED",
        client_secret_hash=hash_client_secret("irrelevant"),
        customer_id=customer.id,
    )
    db_session.add(restricted_client)
    await db_session.flush()

    async with client as c:
        response = await c.get("/v1/item-classes", headers=bearer_header(restricted_client))

    assert response.status_code == 200
    body = response.json()
    assert [row["item_cls_cd"] for row in body] == ["5020150101"]
