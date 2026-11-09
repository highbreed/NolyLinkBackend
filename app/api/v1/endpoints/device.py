"""POST /v1/devices/initialize, admin-only (spec §12, §18)."""

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_auth import require_admin
from app.db.session import get_db
from app.services import cmc_key_manager
from app.services.kra_adapter import KraAdapter, KraError

logger = logging.getLogger(__name__)

router = APIRouter()


class DeviceInitRequest(BaseModel):
    branch_code: str
    tin: str = Field(max_length=11)
    bhf_id: str = Field(max_length=2)
    # OSCU vs VSCU are genuinely different downstream (docs/kra_specs/README.md's 2026-09-01
    # correction) — device_type="VSCU" skips real KRA entirely (no KRA-hosted registration
    # call exists for VSCU) and creates the Branch row locally instead; see
    # app/services/cmc_key_manager.py's _register_vscu_branch_locally. kra_base_url_override is
    # OSCU-only (ignored for VSCU — nothing left to override). Leave both unset for the common
    # OSCU case, which keeps using the global Settings.kra_base_url.
    device_type: Literal["OSCU", "VSCU"] = "OSCU"
    kra_base_url_override: str | None = None
    # Normally omitted — we invent our own (spec §12.2). Set this when KRA has pre-assigned a
    # specific device serial for this tin+bhfId (e.g. a UAT/certification device) that must be
    # used verbatim instead of one we generate.
    dvc_srl_no: str | None = None


class DeviceInitResponse(BaseModel):
    """Deliberately excludes cmc_key — it must never appear in an API response (spec §12.2)."""

    branch_code: str
    tin: str
    bhf_id: str
    status: str
    device_type: str
    kra_base_url_override: str | None
    dvc_id: str | None
    sdc_id: str | None
    mrc_no: str | None


@router.post("/initialize", response_model=DeviceInitResponse, status_code=201)
async def initialize(
    payload: DeviceInitRequest,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> DeviceInitResponse:
    kra = KraAdapter(db=db)
    try:
        branch = await cmc_key_manager.initialize_device(
            db,
            kra,
            payload.branch_code,
            payload.tin,
            payload.bhf_id,
            device_type=payload.device_type,
            kra_base_url_override=payload.kra_base_url_override,
            dvc_srl_no=payload.dvc_srl_no,
        )
        await db.commit()
    except cmc_key_manager.DeviceAlreadyProvisionedButUnknown as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        # device_type=VSCU with no kra_base_url_vscu_{env} configured and no explicit override.
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except KraError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=502, detail=f"KRA error {exc.result_cd}: {exc.result_msg}"
        ) from exc
    finally:
        await kra.aclose()

    logger.info(
        "Device initialized for branch_code=%s tin=%s bhfId=%s by %s",
        branch.branch_code,
        branch.tin,
        branch.bhf_id,
        admin_actor,
    )
    return DeviceInitResponse(
        branch_code=branch.branch_code,
        tin=branch.tin,
        bhf_id=branch.bhf_id,
        status=branch.status,
        device_type=branch.device_type,
        kra_base_url_override=branch.kra_base_url_override,
        dvc_id=branch.dvc_id,
        sdc_id=branch.sdc_id,
        mrc_no=branch.mrc_no,
    )
