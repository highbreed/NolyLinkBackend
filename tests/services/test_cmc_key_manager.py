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
    """An admin pinning an OSCU branch to a specific host (e.g. a tenant with its own dedicated
    KRA gateway) must both call that host for the bootstrap request and persist the override,
    so every later call for this branch (Phases 5-8) keeps using it via
    KraAdapter._resolve_base_url. OSCU-only: kra_base_url_override has no effect for VSCU (no
    KRA-hosted call exists for it at all — see the VSCU tests below).
    """
    override_url = "https://oscu-tenant-y.example/etims-api"
    res_body = load_fixture("device_verification_res")

    with respx.mock() as mock:
        mock.post(f"{override_url}/selectInitOsdcInfo").mock(
            return_value=Response(200, json=res_body)
        )
        branch = await cmc_key_manager.initialize_device(
            db_session,
            kra,
            "OSCU-TENANT-Y",
            "A666666666Z",
            "00",
            kra_base_url_override=override_url,
        )

    assert branch.device_type == "OSCU"
    assert branch.kra_base_url_override == override_url


async def test_initialize_device_vscu_registers_locally_without_calling_kra(db_session, kra):
    """device_type=VSCU has no KRA-hosted registration call at all — decompiling
    vscu-v2.0.21.jar confirms no /selectInitOsdcInfo-equivalent exists for VSCU
    (docs/kra_specs/README.md's 2026-09-01 correction; app/services/cmc_key_manager.py's
    _register_vscu_branch_locally). A respx.mock with no route registered proves it: any real
    HTTP call within this scope fails loudly rather than silently succeeding.
    """
    with respx.mock():
        branch = await cmc_key_manager.initialize_device(
            db_session, kra, "VSCU-STD", "A777777777Z", "00", device_type="VSCU"
        )

    assert branch.device_type == "VSCU"
    assert branch.cmc_key is None
    assert branch.dvc_id is None


async def test_initialize_device_vscu_is_idempotent_by_tin_bhf_id(db_session, kra):
    """Re-registering the same (tin, bhfId) — e.g. resubmitting an onboarding form after fixing
    an unrelated branch's failure — returns the existing local record rather than erroring or
    duplicating it, mirroring initialize_device's own 902-already-provisioned behavior for OSCU.
    """
    with respx.mock():
        first = await cmc_key_manager.initialize_device(
            db_session, kra, "VSCU-DUP", "A999999999Z", "00", device_type="VSCU"
        )
        second = await cmc_key_manager.initialize_device(
            db_session, kra, "VSCU-DUP", "A999999999Z", "00", device_type="VSCU"
        )

    assert second.id == first.id


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
