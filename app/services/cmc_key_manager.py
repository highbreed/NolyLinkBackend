"""Device init + cmcKey lookup, encrypted at rest (spec §12).

cmc_key is never logged here — only branch_code/tin/bhfId identify a device in log lines.
Branch.cmc_key round-trips through EncryptedString transparently, so nothing in this module
ever sees ciphertext directly.
"""

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.models import Branch
from app.schemas.kra.device import DeviceVerificationReq
from app.services.kra_adapter import KraAdapter, KraAuthError

logger = logging.getLogger(__name__)


class DeviceAlreadyProvisionedButUnknown(Exception):
    """KRA returned 902 (device already installed) but we hold no local record for this
    (tin, bhfId) — per spec §10/§12.4, this must escalate to ops rather than being silently
    resolved: the existing cmcKey can't be recovered from KRA, and automatic silent
    re-initialization is explicitly not recommended since it changes a fiscal trust anchor.
    """


def generate_dvc_srl_no(tin: str, bhf_id: str, settings: Settings | None = None) -> str:
    """spec §12.2: we control dvcSrlNo, e.g. "{env}-{tin}-{bhfId}-{uuid4}"."""
    settings = settings or get_settings()
    return f"{settings.kra_env}-{tin}-{bhf_id}-{uuid.uuid4()}"


async def _register_vscu_branch_locally(
    db: AsyncSession, branch_code: str, tin: str, bhf_id: str
) -> Branch:
    """VSCU has no KRA-hosted registration call at all — confirmed by decompiling
    vscu-v2.0.21.jar (docs/kra_specs/README.md's 2026-09-01 correction): the jar's only
    initialization endpoint is /initializer/selectInitInfo against the LOCAL VSCU app
    (app/services/vscu_service.py's VSCUInitializationService.initialize), not any
    /selectInitOsdcInfo-equivalent hosted by KRA. So this never calls real KRA — the Branch row
    is created locally from admin-supplied values, with no cmcKey (the local VSCU app owns its
    own KRA trust anchor internally and never exposes it to this gateway, same as
    VscuDeviceState never storing signKey/intrlKey).

    Idempotent by (tin, bhfId), mirroring initialize_device's own 902-already-provisioned
    handling for OSCU, so re-submitting the same onboarding request after a partial failure is
    safe here too.
    """
    existing = await db.scalar(select(Branch).where(Branch.tin == tin, Branch.bhf_id == bhf_id))
    if existing is not None:
        logger.info(
            "VSCU branch already registered for tin=%s bhfId=%s; using existing record", tin, bhf_id
        )
        return existing

    branch = Branch(
        branch_code=branch_code, tin=tin, bhf_id=bhf_id, cmc_key=None, device_type="VSCU"
    )
    db.add(branch)
    await db.flush()
    logger.info(
        "VSCU branch registered locally for branch_code=%s tin=%s bhfId=%s",
        branch_code,
        tin,
        bhf_id,
    )
    return branch


async def initialize_device(
    db: AsyncSession,
    kra: KraAdapter,
    branch_code: str,
    tin: str,
    bhf_id: str,
    device_type: str = "OSCU",
    kra_base_url_override: str | None = None,
    dvc_srl_no: str | None = None,
) -> Branch:
    """POST /v1/devices/initialize's core logic. Persists cmcKey/dvcId/sdcId/mrcNo into
    `branches`, encrypted at rest, keyed by (tin, bhfId) with branch_code as the only
    identifier client systems and other services ever reference (spec §12.3).

    device_type="VSCU" skips real KRA entirely — see _register_vscu_branch_locally.
    kra_base_url_override is OSCU-only and ignored in that case (nothing left to override).

    dvc_srl_no: normally we invent this ourselves (generate_dvc_srl_no, spec §12.2 — "we
    control dvcSrlNo"), but KRA UAT/certification sometimes pre-assigns a specific physical/
    virtual device serial for a given tin+bhfId that must be used verbatim instead. Pass it
    explicitly in that case; omit it for the normal self-generated case.
    """
    if device_type == "VSCU":
        return await _register_vscu_branch_locally(db, branch_code, tin, bhf_id)

    dvc_srl_no = dvc_srl_no or generate_dvc_srl_no(tin, bhf_id)
    req = DeviceVerificationReq(tin=tin, bhfId=bhf_id, dvcSrlNo=dvc_srl_no)

    try:
        # kra itself resolves the host (explicit override, else device_type's global default,
        # via kra._settings — never the module-level get_settings(), so a KraAdapter built
        # with injected test settings never leaks into real config). Only the explicit
        # override, if any, is persisted below — a device_type-only branch keeps following
        # the global VSCU setting if that's changed later, rather than freezing at init time.
        res = await kra.initialize_device(
            req, base_url_override=kra_base_url_override, device_type=device_type
        )
    except KraAuthError as exc:
        if exc.result_cd != "902":
            raise
        existing = await db.scalar(select(Branch).where(Branch.tin == tin, Branch.bhf_id == bhf_id))
        if existing is not None:
            logger.info(
                "Device already provisioned for tin=%s bhfId=%s; using existing record",
                tin,
                bhf_id,
            )
            return existing
        raise DeviceAlreadyProvisionedButUnknown(
            f"KRA reports tin={tin} bhfId={bhf_id} already has a device, but no local "
            "branch record exists. Escalate to ops rather than re-initializing."
        ) from exc

    if res.data is None:
        raise RuntimeError("KRA returned resultCd=000 for /selectInitOsdcInfo with no data")

    branch = Branch(
        branch_code=branch_code,
        tin=res.data.info.tin,
        bhf_id=res.data.info.bhfId,
        cmc_key=res.data.info.cmcKey,
        dvc_id=res.data.info.dvcId,
        sdc_id=res.data.info.sdcId,
        mrc_no=res.data.info.mrcNo,
        device_type=device_type,
        kra_base_url_override=kra_base_url_override,
    )
    db.add(branch)
    await db.flush()
    logger.info("Device initialized for branch_code=%s tin=%s bhfId=%s", branch_code, tin, bhf_id)
    return branch


async def get_branch_credentials(db: AsyncSession, branch_code: str) -> Branch:
    """Look up a branch's (tin, bhfId, cmcKey) by internal branch_code — the only KRA
    identifier client systems and downstream services ever need to reference (spec §12.3).
    Callers pass branch.cmc_key straight into a KRA request; EncryptedString has already
    decrypted it, so it should still never be logged.
    """
    branch = await db.scalar(select(Branch).where(Branch.branch_code == branch_code))
    if branch is None:
        raise ValueError(f"No branch registered for branch_code={branch_code!r}")
    if branch.status != "ACTIVE":
        raise ValueError(f"Branch {branch_code!r} is not ACTIVE (status={branch.status})")
    return branch
