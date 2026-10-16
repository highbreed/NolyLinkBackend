"""Admin-only read surface for the frontend admin portal: branch/device health and a merged
call-log viewer across both protocols. Everything here is a read of data every other endpoint
already writes (Branch, VscuDeviceState, KraCallLog, VscuCallLog) — no new business logic.

Pagination note: call-log listing supports `limit` (most recent N, newest first) but not a
true offset across the two heterogeneous log tables (KraCallLog/VscuCallLog) — acceptable for
a first cut of "what's happening right now"; a cursor-based version can follow if the
dashboard needs to page deep into history.
"""

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_auth import require_admin
from app.db.models import Branch, KraCallLog, VscuCallLog, VscuDeviceState
from app.db.session import get_db

router = APIRouter(dependencies=[Depends(require_admin)])


class VscuHealthOut(BaseModel):
    base_url: str
    initialized_at: datetime | None
    last_sequence_sync_at: datetime | None
    last_known_invc_no: int | None
    last_known_sale_rcpt_no: int | None
    last_successful_call_at: datetime | None
    last_error: str | None


class BranchAdminOut(BaseModel):
    id: uuid.UUID
    branch_code: str
    tin: str
    bhf_id: str
    device_type: str
    status: str
    created_at: datetime
    vscu: VscuHealthOut | None


def _to_vscu_health_out(state: VscuDeviceState) -> VscuHealthOut:
    return VscuHealthOut(
        base_url=state.base_url,
        initialized_at=state.initialized_at,
        last_sequence_sync_at=state.last_sequence_sync_at,
        last_known_invc_no=state.last_known_invc_no,
        last_known_sale_rcpt_no=state.last_known_sale_rcpt_no,
        last_successful_call_at=state.last_successful_call_at,
        last_error=state.last_error,
    )


@router.get("/branches", response_model=list[BranchAdminOut])
async def list_branches(db: AsyncSession = Depends(get_db)) -> list[BranchAdminOut]:
    branches = (await db.execute(select(Branch).order_by(Branch.created_at.desc()))).scalars().all()
    states = {s.branch_id: s for s in (await db.execute(select(VscuDeviceState))).scalars().all()}
    return [
        BranchAdminOut(
            id=b.id,
            branch_code=b.branch_code,
            tin=b.tin,
            bhf_id=b.bhf_id,
            device_type=b.device_type,
            status=b.status,
            created_at=b.created_at,
            vscu=_to_vscu_health_out(states[b.id]) if b.id in states else None,
        )
        for b in branches
    ]


@router.get("/branches/{branch_code}", response_model=BranchAdminOut)
async def get_branch(branch_code: str, db: AsyncSession = Depends(get_db)) -> BranchAdminOut:
    branch = await db.scalar(select(Branch).where(Branch.branch_code == branch_code))
    if branch is None:
        raise HTTPException(status_code=404, detail="No branch with that branch_code")
    state = await db.scalar(select(VscuDeviceState).where(VscuDeviceState.branch_id == branch.id))
    return BranchAdminOut(
        id=branch.id,
        branch_code=branch.branch_code,
        tin=branch.tin,
        bhf_id=branch.bhf_id,
        device_type=branch.device_type,
        status=branch.status,
        created_at=branch.created_at,
        vscu=_to_vscu_health_out(state) if state is not None else None,
    )


class CallLogEntry(BaseModel):
    protocol: Literal["OSCU", "VSCU"]
    id: int
    branch_code: str | None
    endpoint: str
    result_cd: str | None
    result_msg: str | None
    result_dt: str | None
    http_status: int | None = None
    latency_ms: int | None = None
    retry_count: int | None = None
    created_at: datetime


@router.get("/call-logs", response_model=list[CallLogEntry])
async def list_call_logs(
    branch_code: str | None = None,
    protocol: Literal["OSCU", "VSCU"] | None = None,
    result_cd: str | None = None,
    since: datetime | None = None,
    limit: int = Query(default=50, le=200, ge=1),
    db: AsyncSession = Depends(get_db),
) -> list[CallLogEntry]:
    branch_id: uuid.UUID | None = None
    if branch_code is not None:
        branch = await db.scalar(select(Branch).where(Branch.branch_code == branch_code))
        if branch is None:
            raise HTTPException(status_code=404, detail="No branch with that branch_code")
        branch_id = branch.id

    branch_codes_by_id = {
        row.id: row.branch_code
        for row in (await db.execute(select(Branch.id, Branch.branch_code))).all()
    }

    entries: list[CallLogEntry] = []

    if protocol in (None, "OSCU"):
        kra_stmt = select(KraCallLog).order_by(KraCallLog.created_at.desc()).limit(limit)
        if branch_id is not None:
            kra_stmt = kra_stmt.where(KraCallLog.branch_id == branch_id)
        if result_cd is not None:
            kra_stmt = kra_stmt.where(KraCallLog.result_cd == result_cd)
        if since is not None:
            kra_stmt = kra_stmt.where(KraCallLog.created_at >= since)
        for kra_row in (await db.execute(kra_stmt)).scalars().all():
            entries.append(
                CallLogEntry(
                    protocol="OSCU",
                    id=kra_row.id,
                    branch_code=(
                        branch_codes_by_id.get(kra_row.branch_id) if kra_row.branch_id else None
                    ),
                    endpoint=kra_row.endpoint,
                    result_cd=kra_row.result_cd,
                    result_msg=kra_row.result_msg,
                    result_dt=kra_row.result_dt,
                    retry_count=kra_row.retry_count,
                    created_at=kra_row.created_at,
                )
            )

    if protocol in (None, "VSCU"):
        vscu_stmt = select(VscuCallLog).order_by(VscuCallLog.created_at.desc()).limit(limit)
        if branch_id is not None:
            vscu_stmt = vscu_stmt.where(VscuCallLog.branch_id == branch_id)
        if result_cd is not None:
            vscu_stmt = vscu_stmt.where(VscuCallLog.result_cd == result_cd)
        if since is not None:
            vscu_stmt = vscu_stmt.where(VscuCallLog.created_at >= since)
        for vscu_row in (await db.execute(vscu_stmt)).scalars().all():
            entries.append(
                CallLogEntry(
                    protocol="VSCU",
                    id=vscu_row.id,
                    branch_code=(
                        branch_codes_by_id.get(vscu_row.branch_id) if vscu_row.branch_id else None
                    ),
                    endpoint=vscu_row.endpoint,
                    result_cd=vscu_row.result_cd,
                    result_msg=vscu_row.result_msg,
                    result_dt=vscu_row.result_dt,
                    http_status=vscu_row.http_status,
                    latency_ms=vscu_row.latency_ms,
                    created_at=vscu_row.created_at,
                )
            )

    entries.sort(key=lambda e: e.created_at, reverse=True)
    return entries[:limit]
