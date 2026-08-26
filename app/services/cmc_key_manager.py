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


async def initialize_device(
    db: AsyncSession, kra: KraAdapter, branch_code: str, tin: str, bhf_id: str
) -> Branch:
    """POST /v1/devices/initialize's core logic. Persists cmcKey/dvcId/sdcId/mrcNo into
    `branches`, encrypted at rest, keyed by (tin, bhfId) with branch_code as the only
    identifier client systems and other services ever reference (spec §12.3).
    """
    dvc_srl_no = generate_dvc_srl_no(tin, bhf_id)
    req = DeviceVerificationReq(tin=tin, bhfId=bhf_id, dvcSrlNo=dvc_srl_no)

    try:
        res = await kra.initialize_device(req)
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
        tin=res.data.taxpayer.tin,
        bhf_id=res.data.branch.bhfId,
        cmc_key=res.data.device.cmcKey,
        dvc_id=res.data.device.dvcId,
        sdc_id=res.data.device.sdcId,
        mrc_no=res.data.device.mrcNo,
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
