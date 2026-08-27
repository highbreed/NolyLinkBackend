"""POST /v1/devices/initialize, admin-only (spec §12, §18)."""

from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_db
from app.services import cmc_key_manager
from app.services.kra_adapter import KraAdapter, KraError

router = APIRouter()


async def require_admin(x_admin_api_key: str = Header(default="")) -> None:
    """Provisional admin gate — Phase 9 replaces this with the full client authN/Z system.
    This endpoint must never be reachable by SAP/ERP clients (spec §12.2).
    """
    configured = get_settings().admin_api_key
    if not configured or x_admin_api_key != configured:
        raise HTTPException(status_code=403, detail="Admin access required")


class DeviceInitRequest(BaseModel):
    branch_code: str
    tin: str = Field(max_length=11)
    bhf_id: str = Field(max_length=2)
    # OSCU and VSCU share the exact same request/response contract — only the host differs.
    # device_type is purely descriptive (surfaced back for admin/billing visibility);
    # kra_base_url_override is what actually redirects this branch's calls. Leave both unset
    # for the common OSCU case, which keeps using the global Settings.kra_base_url.
    device_type: Literal["OSCU", "VSCU"] = "OSCU"
    kra_base_url_override: str | None = None


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


@router.post(
    "/initialize",
    response_model=DeviceInitResponse,
    status_code=201,
    dependencies=[Depends(require_admin)],
)
async def initialize(
    payload: DeviceInitRequest,
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
