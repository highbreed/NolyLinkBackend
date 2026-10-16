"""VSCU Z-report admin/diagnostic API — POST /v1/vscu/{branch_id}/reports/z/save,
GET /v1/vscu/{branch_id}/reports/z/{rpt_de}. Split from vscu_reference_data.py purely to keep
each file a manageable size; mounts under the same /v1/vscu prefix and shares its admin gate.

No persistence table: the local VSCU app is the source of truth for Z-report content (it
builds the report itself from its own tracked receipt history — see
app/integrations/vscu/schemas/report.py's module docstring) — these endpoints are a thin
pass-through, not a cache.
"""

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_auth import require_admin
from app.db.session import get_db
from app.integrations.vscu.client import VSCUClient
from app.integrations.vscu.exceptions import VSCUError
from app.integrations.vscu.schemas.report import ReportZCheckReq, ReportZSaveReq
from app.services.vscu_service import _get_branch

logger = logging.getLogger(__name__)

router = APIRouter()


def _client(db: AsyncSession) -> VSCUClient:
    return VSCUClient(db=db)


def _http_error(exc: VSCUError) -> HTTPException:
    # str(exc), not exc.context.result_cd/result_msg: VSCUConfigurationError/VSCUTransportError
    # never carry those (no request was ever attempted, or none ever got a VSCU envelope back).
    return HTTPException(status_code=502, detail=f"VSCU error: {exc}")


@router.post("/{branch_id}/reports/z/save")
async def save_z_report(
    branch_id: uuid.UUID,
    rpt_de: str,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str | None]:
    """rpt_de: the report date, YYYYMMDD (spec's rptDe)."""
    client = _client(db)
    try:
        branch = await _get_branch(db, branch_id)
        req = ReportZSaveReq(tin=branch.tin, bhfId=branch.bhf_id, rptDe=rpt_de)
        res = await client.save_z_report(branch_id, req)
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except VSCUError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    finally:
        await client.aclose()
    return {"result_cd": res.resultCd, "result_msg": res.resultMsg}


@router.get("/{branch_id}/reports/z/{rpt_de}")
async def check_z_report(
    branch_id: uuid.UUID,
    rpt_de: str,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    client = _client(db)
    try:
        branch = await _get_branch(db, branch_id)
        req = ReportZCheckReq(tin=branch.tin, bhfId=branch.bhf_id, rptDe=rpt_de)
        res = await client.check_z_report(branch_id, req)
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except VSCUError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    finally:
        await client.aclose()
    return res.model_dump(mode="json")
