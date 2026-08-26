"""API tests for POST /v1/branches/customers, /users, /insurance (spec §18)."""

import pytest
import respx
from httpx import ASGITransport, AsyncClient, Response

from app.core.config import get_settings
from app.db.models import Branch
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
async def branch(db_session) -> Branch:
    b = Branch(branch_code="NAIROBI-HQ", tin="A123456789Z", bhf_id="00", cmc_key="c" * 10)
    db_session.add(b)
    await db_session.flush()
    return b


def success_envelope():
    return {
        "resultCd": "000",
        "resultMsg": "It is succeeded",
        "resultDt": "20200226143124",
        "data": None,
    }


async def test_save_branch_customer(client, branch):
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
            )
    assert response.status_code == 201
    assert response.json()["resultCd"] == "000"


async def test_save_branch_user(client, branch):
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
            )
    assert response.status_code == 201


async def test_save_branch_insurance(client, branch):
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
            )
    assert response.status_code == 201


async def test_save_branch_customer_unknown_branch_is_404(client):
    async with client as c:
        response = await c.post(
            "/v1/branches/customers",
            json={
                "branch_code": "DOES-NOT-EXIST",
                "cust_no": "000000001",
                "cust_tin": "P123456789A",
                "cust_nm": "John Customer",
            },
        )
    assert response.status_code == 404
