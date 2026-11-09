"""Admin-only read surface over client_call_log (app/db/models.py) — "what did a client
actually send us, and what did we send back", persisted by AuditLogMiddleware
(app/core/audit.py) on every request. Distinct from admin_dashboard.py's call-log viewer,
which shows this gateway's own outbound calls TO KRA/VSCU, not inbound calls FROM clients.

Real offset pagination (Page[ClientCallLogOut], matching the frontend's existing Page<T>
convention) since this table can get large — unlike most of today's admin surface, which only
supports "most recent N".
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_auth import require_admin
from app.db.models import ClientCallLog
from app.db.session import get_db
from app.schemas.page import Page

router = APIRouter(dependencies=[Depends(require_admin)])


class ClientCallLogOut(BaseModel):
    id: int
    request_id: str
    client_id: uuid.UUID | None
    method: str
    path: str
    status_code: int
    duration_ms: int
    request_body: object | None
    response_body: object | None
    created_at: datetime


def _to_out(row: ClientCallLog) -> ClientCallLogOut:
    return ClientCallLogOut(
        id=row.id,
        request_id=row.request_id,
        client_id=row.client_id,
        method=row.method,
        path=row.path,
        status_code=row.status_code,
        duration_ms=row.duration_ms,
        request_body=row.request_body,
        response_body=row.response_body,
        created_at=row.created_at,
    )


@router.get("", response_model=Page[ClientCallLogOut])
async def list_client_calls(
    client_id: uuid.UUID | None = None,
    path: str | None = Query(default=None, description="Prefix match, e.g. /v1/sales-invoices"),
    method: str | None = None,
    status_code: int | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, le=200, ge=1),
    db: AsyncSession = Depends(get_db),
) -> Page[ClientCallLogOut]:
    stmt = select(ClientCallLog)
    if client_id is not None:
        stmt = stmt.where(ClientCallLog.client_id == client_id)
    if path is not None:
        stmt = stmt.where(ClientCallLog.path.startswith(path))
    if method is not None:
        stmt = stmt.where(ClientCallLog.method == method.upper())
    if status_code is not None:
        stmt = stmt.where(ClientCallLog.status_code == status_code)
    if since is not None:
        stmt = stmt.where(ClientCallLog.created_at >= since)
    if until is not None:
        stmt = stmt.where(ClientCallLog.created_at <= until)

    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))

    stmt = (
        stmt.order_by(ClientCallLog.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = (await db.execute(stmt)).scalars().all()

    return Page[ClientCallLogOut](
        items=[_to_out(row) for row in rows],
        total=total or 0,
        page=page,
        page_size=page_size,
    )
