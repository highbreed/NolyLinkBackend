"""VSCU integration layer admin/diagnostic API — POST /v1/vscu/{branch_id}/initialize,
/echo, /sequences/sync, /sequences/update, GET /health, /server-time.

Admin-only (spec §51: "All diagnostic APIs must be authentication/authorization protected"),
same gate as POST /v1/devices/initialize. Every branch_id here must already exist (created via
POST /v1/devices/initialize with device_type=VSCU) — this router only manages the VSCU-
specific device state layered on top of that Branch row, mirroring cmc_key_manager's
"Branch already exists" assumption for the OSCU flow.

Every VSCUError handler below reports str(exc), never exc.context.result_cd/result_msg
directly (2026-09-02 fix — was producing "VSCU error None: None"): VSCUConfigurationError and
VSCUTransportError never carry a real resultCd/resultMsg (no request was ever attempted, or it
failed before any VSCU envelope existed to inspect), so str(exc) is the only formatting that's
correct for every VSCUError subtype — it already includes resultCd/resultMsg when there is one.
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_auth import require_admin
from app.db.models import Branch, VscuDeviceState
from app.db.session import get_db
from app.integrations.vscu.client import VSCUClient
from app.integrations.vscu.exceptions import VSCUError
from app.services.vscu_service import VSCUInitializationService, VSCUTestService

logger = logging.getLogger(__name__)

router = APIRouter()


def _mask_tin(tin: str) -> str:
    if len(tin) <= 4:
        return "*" * len(tin)
    return f"{'*' * (len(tin) - 4)}{tin[-4:]}"


class VSCUInitializeRequest(BaseModel):
    # Each branch's VSCU device runs on that client's own network — see
    # app/db/models.py VscuDeviceState docstring for why this is per-request, not a global
    # setting like kra_base_url is for OSCU.
    base_url: str = Field(
        description='Local VSCU app URL for this branch, e.g. "http://192.168.1.10:8088"'
    )
    # KRA issues this once, domain-wide, to the integration — not per branch/customer — so it
    # defaults from Settings.vscu_dvc_srl_no_for_env() when omitted (see that setting's
    # docstring). Only pass this explicitly to override the default for one device.
    dvc_srl_no: str | None = Field(
        default=None,
        description=(
            "KRA-issued domain-wide VSCU serial number. Omit to use the configured default."
        ),
    )
    connect_timeout_seconds: float | None = None
    read_timeout_seconds: float | None = None


class VSCUDeviceStateOut(BaseModel):
    """Deliberately excludes cmcKey/signKey/intrlKey — the local VSCU app never gives them to
    us in the first place (verified, see app/integrations/vscu/schemas/init.py), so there is
    nothing here to accidentally leak even if a field were added carelessly later.
    """

    branch_id: uuid.UUID
    base_url: str
    dvc_srl_no: str | None
    sdc_id: str | None
    mrc_no: str | None
    dvc_id: str | None
    vat_ty_cd: str | None
    initialized_at: str | None


class VSCUEchoRequest(BaseModel):
    test_str: str = "PING"


class VSCUEchoResponse(BaseModel):
    result_cd: str
    result_msg: str | None
    echoed: str | None


class VSCUHealthResponse(BaseModel):
    configured: bool
    reachable: bool
    tin: str | None
    bhf_id: str | None
    sdc_id: str | None
    mrc_no: str | None
    last_successful_call_at: str | None
    last_error: str | None


def _client(db: AsyncSession) -> VSCUClient:
    return VSCUClient(db=db)


@router.post("/{branch_id}/initialize", response_model=VSCUDeviceStateOut, status_code=201)
async def initialize(
    branch_id: uuid.UUID,
    payload: VSCUInitializeRequest,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> VSCUDeviceStateOut:
    client = _client(db)
    try:
        service = VSCUInitializationService(db, client)
        device = await service.initialize(
            branch_id,
            base_url=payload.base_url,
            dvc_srl_no=payload.dvc_srl_no,
            connect_timeout_seconds=payload.connect_timeout_seconds,
            read_timeout_seconds=payload.read_timeout_seconds,
        )
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except VSCUError as exc:
        await db.rollback()
        # str(exc), not exc.context.result_cd/result_msg — see module docstring.
        raise HTTPException(status_code=502, detail=f"VSCU error: {exc}") from exc
    finally:
        await client.aclose()

    logger.info("VSCU initialized for branch_id=%s by %s", branch_id, admin_actor)
    return VSCUDeviceStateOut(
        branch_id=device.branch_id,
        base_url=device.base_url,
        dvc_srl_no=device.dvc_srl_no,
        sdc_id=device.sdc_id,
        mrc_no=device.mrc_no,
        dvc_id=device.dvc_id,
        vat_ty_cd=device.vat_ty_cd,
        initialized_at=device.initialized_at.isoformat() if device.initialized_at else None,
    )


@router.post("/{branch_id}/echo", response_model=VSCUEchoResponse)
async def echo(
    branch_id: uuid.UUID,
    payload: VSCUEchoRequest,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> VSCUEchoResponse:
    client = _client(db)
    try:
        service = VSCUTestService(db, client)
        res = await service.echo(branch_id, test_str=payload.test_str)
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except VSCUError as exc:
        await db.rollback()
        # str(exc), not exc.context.result_cd/result_msg — see module docstring.
        raise HTTPException(status_code=502, detail=f"VSCU error: {exc}") from exc
    finally:
        await client.aclose()

    return VSCUEchoResponse(
        result_cd=res.resultCd,
        result_msg=res.resultMsg,
        echoed=res.data.testStr if res.data else None,
    )


@router.get("/{branch_id}/server-time")
async def server_time(
    branch_id: uuid.UUID,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    client = _client(db)
    try:
        service = VSCUTestService(db, client)
        raw = await service.server_time(branch_id)
        await db.commit()
    except VSCUError as exc:
        await db.rollback()
        # str(exc), not exc.context.result_cd/result_msg — see module docstring.
        raise HTTPException(status_code=502, detail=f"VSCU error: {exc}") from exc
    finally:
        await client.aclose()
    return {"server_time": raw}


@router.post("/{branch_id}/sequences/sync")
async def sync_sequences(
    branch_id: uuid.UUID,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str | None]:
    client = _client(db)
    try:
        service = VSCUInitializationService(db, client)
        res = await service.sync_sequences(branch_id)
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except VSCUError as exc:
        await db.rollback()
        # str(exc), not exc.context.result_cd/result_msg — see module docstring.
        raise HTTPException(status_code=502, detail=f"VSCU error: {exc}") from exc
    finally:
        await client.aclose()
    return {"result_cd": res.resultCd, "result_msg": res.resultMsg}


@router.post("/{branch_id}/sequences/update")
async def update_sequences(
    branch_id: uuid.UUID,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str | None]:
    """Purely local reconciliation on the VSCU app's own sequence counters — use after a
    VSCUSequenceError (836), per spec §9, before resubmitting anything.
    """
    client = _client(db)
    try:
        service = VSCUInitializationService(db, client)
        res = await service.update_sequences(branch_id)
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except VSCUError as exc:
        await db.rollback()
        # str(exc), not exc.context.result_cd/result_msg — see module docstring.
        raise HTTPException(status_code=502, detail=f"VSCU error: {exc}") from exc
    finally:
        await client.aclose()
    return {"result_cd": res.resultCd, "result_msg": res.resultMsg}


@router.get("/{branch_id}/health", response_model=VSCUHealthResponse)
async def health(
    branch_id: uuid.UUID,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> VSCUHealthResponse:
    """configured: a VscuDeviceState row exists. reachable: /main/selectServerTime returned a
    non-empty string — that endpoint swallows its own errors into "" (verified,
    MainExcute.java), so an empty/failed response is the only signal available without making
    a full echoTest round-trip (which itself hits real KRA, not just the local app).
    """
    device = await db.scalar(select(VscuDeviceState).where(VscuDeviceState.branch_id == branch_id))
    if device is None:
        return VSCUHealthResponse(
            configured=False,
            reachable=False,
            tin=None,
            bhf_id=None,
            sdc_id=None,
            mrc_no=None,
            last_successful_call_at=None,
            last_error=None,
        )

    branch = await db.get(Branch, branch_id)
    client = _client(db)
    reachable = False
    try:
        service = VSCUTestService(db, client)
        raw = await service.server_time(branch_id)
        reachable = bool(raw)
        await db.commit()
    except VSCUError:
        await db.rollback()
        reachable = False
    finally:
        await client.aclose()

    return VSCUHealthResponse(
        configured=True,
        reachable=reachable,
        tin=_mask_tin(branch.tin) if branch else None,
        bhf_id=branch.bhf_id if branch else None,
        sdc_id=device.sdc_id,
        mrc_no=device.mrc_no,
        last_successful_call_at=(
            device.last_successful_call_at.isoformat() if device.last_successful_call_at else None
        ),
        last_error=device.last_error,
    )
