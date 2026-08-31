"""Tests for cmc_key_manager (spec §12): device init, encryption at rest, and the 902
(already-provisioned) special case.
"""

import json
import logging
from pathlib import Path

import pytest
import respx
from httpx import Response
from sqlalchemy import text

from app.core.config import Settings
from app.core.security import get_encryption_backend
from app.db.models import Branch
from app.services import cmc_key_manager
from app.services.kra_adapter import KraAdapter

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures" / "kra_samples"
BASE_URL = "https://kra-sandbox.test/etims-api"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text())


@pytest.fixture
def adapter_settings() -> Settings:
    return Settings(kra_env="sandbox", kra_base_url_sbx=f"{BASE_URL}/")


@pytest.fixture
def kra(db_session, adapter_settings: Settings) -> KraAdapter:
    return KraAdapter(db=db_session, settings=adapter_settings)


async def test_initialize_device_persists_encrypted_cmc_key(db_session, kra, caplog):
    res_body = load_fixture("device_verification_res")
    plaintext_cmc_key = res_body["data"]["info"]["cmcKey"]

    with caplog.at_level(logging.INFO), respx.mock(base_url=BASE_URL) as mock:
        mock.post("/selectInitOsdcInfo").mock(return_value=Response(200, json=res_body))
        branch = await cmc_key_manager.initialize_device(
            db_session, kra, "NAIROBI-HQ", "A123456789Z", "00"
        )
    await db_session.flush()

    # ORM-level read is transparently decrypted back to the plaintext KRA returned.
    assert branch.cmc_key == plaintext_cmc_key

    # The raw DB column must never hold the plaintext — bypass the ORM to check the ciphertext.
    raw = await db_session.execute(
        text("SELECT cmc_key FROM branches WHERE id = :id"), {"id": branch.id}
    )
    stored = raw.scalar_one()
    assert stored != plaintext_cmc_key
    assert get_encryption_backend().decrypt(stored) == plaintext_cmc_key

    # Nothing logged during this flow contains the plaintext cmcKey.
    for record in caplog.records:
        assert plaintext_cmc_key not in record.getMessage()


async def test_902_with_existing_branch_returns_it(db_session, kra):
    existing = Branch(
        branch_code="NAIROBI-HQ", tin="A123456789Z", bhf_id="00", cmc_key="already-here"
    )
    db_session.add(existing)
    await db_session.flush()

    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/selectInitOsdcInfo").mock(
            return_value=Response(
                200,
                json={
                    "resultCd": "902",
                    "resultMsg": "Device already installed",
                    "resultDt": "20200226143124",
                    "data": None,
                },
            )
        )
        branch = await cmc_key_manager.initialize_device(
            db_session, kra, "NAIROBI-HQ", "A123456789Z", "00"
        )

    assert branch.id == existing.id
    assert branch.cmc_key == "already-here"


async def test_902_without_existing_branch_escalates(db_session, kra):
    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/selectInitOsdcInfo").mock(
            return_value=Response(
                200,
                json={
                    "resultCd": "902",
                    "resultMsg": "Device already installed",
                    "resultDt": "20200226143124",
                    "data": None,
                },
            )
        )
        with pytest.raises(cmc_key_manager.DeviceAlreadyProvisionedButUnknown):
            await cmc_key_manager.initialize_device(
                db_session, kra, "MOMBASA-01", "A987654321Z", "00"
            )


async def test_other_auth_errors_propagate(db_session, kra):
    from app.services.kra_adapter import KraAuthError

    with respx.mock(base_url=BASE_URL) as mock:
        mock.post("/selectInitOsdcInfo").mock(
            return_value=Response(
                200,
                json={
                    "resultCd": "901",
                    "resultMsg": "Invalid device",
                    "resultDt": "20200226143124",
                    "data": None,
                },
            )
        )
        with pytest.raises(KraAuthError):
            await cmc_key_manager.initialize_device(
                db_session, kra, "KISUMU-01", "A111111111Z", "00"
            )


async def test_initialize_device_with_explicit_override_persists_it_and_calls_that_host(
    db_session, kra
):
    """An admin pinning a specific branch to a specific host (e.g. a VSCU tenant on its own
    endpoint) must both call that host for the bootstrap request and persist the override, so
    every later call for this branch (Phases 5-8) keeps using it via KraAdapter._resolve_base_url.
    """
    override_url = "https://vscu-tenant-y.example/etims-api"
    res_body = load_fixture("device_verification_res")

    with respx.mock() as mock:
        mock.post(f"{override_url}/selectInitOsdcInfo").mock(
            return_value=Response(200, json=res_body)
        )
        branch = await cmc_key_manager.initialize_device(
            db_session,
            kra,
            "VSCU-TENANT-Y",
            "A666666666Z",
            "00",
            device_type="VSCU",
            kra_base_url_override=override_url,
        )

    assert branch.device_type == "VSCU"
    assert branch.kra_base_url_override == override_url


async def test_initialize_device_vscu_uses_configured_global_default(db_session):
    """device_type=VSCU with no per-branch override falls back to the global
    kra_base_url_vscu_sbx setting — and does NOT persist a resolved URL onto the branch, so a
    later change to that global setting is still picked up (see kra_adapter._resolve_base_url).
    """
    vscu_url = "https://vscu.kra-sandbox.test/etims-api"
    vscu_settings = Settings(
        kra_env="sandbox", kra_base_url_sbx=f"{BASE_URL}/", kra_base_url_vscu_sbx=vscu_url
    )
    vscu_kra = KraAdapter(db=db_session, settings=vscu_settings)
    res_body = load_fixture("device_verification_res")

    with respx.mock() as mock:
        mock.post(f"{vscu_url}/selectInitOsdcInfo").mock(return_value=Response(200, json=res_body))
        branch = await cmc_key_manager.initialize_device(
            db_session, vscu_kra, "VSCU-STD", "A777777777Z", "00", device_type="VSCU"
        )

    assert branch.device_type == "VSCU"
    assert branch.kra_base_url_override is None


async def test_initialize_device_vscu_without_configured_url_raises(db_session, kra):
    """adapter_settings (the default test fixture) never sets kra_base_url_vscu_sbx — this
    must fail loudly rather than silently POSTing to an empty host.
    """
    with pytest.raises(ValueError, match="kra_base_url_vscu_sandbox"):
        await cmc_key_manager.initialize_device(
            db_session, kra, "VSCU-UNCONFIGURED", "A888888888Z", "00", device_type="VSCU"
        )


async def test_get_branch_credentials_round_trips_cmc_key(db_session):
    branch = Branch(branch_code="ELDORET-01", tin="A222222222Z", bhf_id="00", cmc_key="k" * 255)
    db_session.add(branch)
    await db_session.flush()

    fetched = await cmc_key_manager.get_branch_credentials(db_session, "ELDORET-01")
    assert fetched.cmc_key == "k" * 255


async def test_get_branch_credentials_rejects_unknown_branch(db_session):
    with pytest.raises(ValueError, match="No branch registered"):
        await cmc_key_manager.get_branch_credentials(db_session, "DOES-NOT-EXIST")


async def test_get_branch_credentials_rejects_inactive_branch(db_session):
    branch = Branch(
        branch_code="SUSPENDED-01",
        tin="A333333333Z",
        bhf_id="00",
        cmc_key="k",
        status="SUSPENDED",
    )
    db_session.add(branch)
    await db_session.flush()

    with pytest.raises(ValueError, match="not ACTIVE"):
        await cmc_key_manager.get_branch_credentials(db_session, "SUSPENDED-01")
