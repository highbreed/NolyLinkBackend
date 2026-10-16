"""API tests for POST /v1/branches/customers, /users, /insurance (spec §18)."""

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from app.core.config import get_settings
from app.db.models import Branch, Client, Customer
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
async def branch(db_session) -> Branch:
    b = Branch(branch_code="NAIROBI-HQ", tin="A123456789Z", bhf_id="00", cmc_key="c" * 10)
    db_session.add(b)
    await db_session.flush()
    return b


@pytest.fixture
async def auth_headers(db_session, branch) -> dict[str, str]:
    customer = Customer(name="SAP-TEST", allowed_branch_ids=[branch.id])
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


@pytest.fixture
async def auth_headers_no_branches(db_session) -> dict[str, str]:
    customer = Customer(name="SAP-TEST-EMPTY", allowed_branch_ids=[])
    db_session.add(customer)
    await db_session.flush()
    registered_client = Client(
        name="SAP-TEST-EMPTY",
        client_secret_hash=hash_client_secret("irrelevant"),
        customer_id=customer.id,
    )
    db_session.add(registered_client)
    await db_session.flush()
    return bearer_header(registered_client)


def success_envelope():
    return {
        "resultCd": "000",
        "resultMsg": "It is succeeded",
        "resultDt": "20200226143124",
        "data": None,
    }


async def test_save_branch_customer(client, branch, auth_headers):
    kra_base_url = get_settings().kra_base_url
    with respx.mock(base_url=kra_base_url) as mock:
        mock.post("/saveBhfCustomer").mock(return_value=Response(200, json=success_envelope()))
        async with client as c:
            response = await c.post(
                "/v1/branches/customers",
                json={
                    "branch_code": "NAIROBI-HQ",
                    "cust_no": "000000001",
                    "cust_tin": "P123456789A",
                    "cust_nm": "John Customer",
                },
                headers=auth_headers,
            )
    assert response.status_code == 201
    assert response.json()["resultCd"] == "000"


async def test_save_branch_user(client, branch, auth_headers):
    kra_base_url = get_settings().kra_base_url
    with respx.mock(base_url=kra_base_url) as mock:
        mock.post("/saveBhfUser").mock(return_value=Response(200, json=success_envelope()))
        async with client as c:
            response = await c.post(
                "/v1/branches/users",
                json={
                    "branch_code": "NAIROBI-HQ",
                    "user_id": "cashier01",
                    "user_nm": "Cashier One",
                    "pwd": "p" * 12,
                },
                headers=auth_headers,
            )
    assert response.status_code == 201


async def test_save_branch_insurance(client, branch, auth_headers):
    kra_base_url = get_settings().kra_base_url
    with respx.mock(base_url=kra_base_url) as mock:
        mock.post("/saveBhfInsurance").mock(return_value=Response(200, json=success_envelope()))
        async with client as c:
            response = await c.post(
                "/v1/branches/insurance",
                json={
                    "branch_code": "NAIROBI-HQ",
                    "isrcc_cd": "INS001",
                    "isrcc_nm": "NHIF",
                    "isrc_rt": "100",
                },
                headers=auth_headers,
            )
    assert response.status_code == 201


async def test_save_branch_customer_unknown_branch_is_404(client, auth_headers_no_branches):
    async with client as c:
        response = await c.post(
            "/v1/branches/customers",
            json={
                "branch_code": "DOES-NOT-EXIST",
                "cust_no": "000000001",
                "cust_tin": "P123456789A",
                "cust_nm": "John Customer",
            },
            headers=auth_headers_no_branches,
        )
    assert response.status_code == 404


async def test_save_branch_customer_without_token_is_401(client, branch):
    async with client as c:
        response = await c.post(
            "/v1/branches/customers",
            json={
                "branch_code": "NAIROBI-HQ",
                "cust_no": "000000001",
                "cust_tin": "P123456789A",
                "cust_nm": "John Customer",
            },
        )
    assert response.status_code == 401


async def test_save_branch_customer_disallowed_branch_is_403(
    client, branch, auth_headers_no_branches
):
    async with client as c:
        response = await c.post(
            "/v1/branches/customers",
            json={
                "branch_code": "NAIROBI-HQ",
                "cust_no": "000000001",
                "cust_tin": "P123456789A",
                "cust_nm": "John Customer",
            },
            headers=auth_headers_no_branches,
        )
    assert response.status_code == 403
