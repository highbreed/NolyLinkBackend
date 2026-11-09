"""Real backend for the admin portal's global audit log — a queryable record of admin
actions ("admin X suspended customer Y"), previously only scattered stdout logger.info()
calls (see app/services/admin_audit.py, app/db/models.py's AdminAuditLog).
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_auth import require_admin
from app.db.models import AdminAuditLog
from app.db.session import get_db
from app.schemas.page import Page

router = APIRouter(dependencies=[Depends(require_admin)])


class AuditLogEntryOut(BaseModel):
    id: int
    admin_user_email: str | None
    action: str
    target_type: str
    target_id: str
    before_state: dict[str, object] | None
    after_state: dict[str, object] | None
    reason: str | None
    ip_address: str | None
    request_id: str | None
    created_at: datetime


def _to_out(row: AdminAuditLog) -> AuditLogEntryOut:
    return AuditLogEntryOut(
        id=row.id,
        admin_user_email=row.admin_user_email,
        action=row.action,
        target_type=row.target_type,
        target_id=row.target_id,
        before_state=row.before_state,
        after_state=row.after_state,
        reason=row.reason,
        ip_address=row.ip_address,
        request_id=row.request_id,
        created_at=row.created_at,
    )


@router.get("", response_model=Page[AuditLogEntryOut])
async def list_audit_log(
    target_type: str | None = None,
    target_id: str | None = None,
    q: str | None = Query(default=None, description="Free text over action/admin_user_email"),
    since: datetime | None = None,
    until: datetime | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, le=200, ge=1),
    db: AsyncSession = Depends(get_db),
) -> Page[AuditLogEntryOut]:
    stmt = select(AdminAuditLog)
    if target_type is not None:
        stmt = stmt.where(AdminAuditLog.target_type == target_type)
    if target_id is not None:
        stmt = stmt.where(AdminAuditLog.target_id == target_id)
    if q:
        needle = f"%{q}%"
        stmt = stmt.where(
            or_(
                AdminAuditLog.action.ilike(needle),
                AdminAuditLog.admin_user_email.ilike(needle),
            )
        )
    if since is not None:
        stmt = stmt.where(AdminAuditLog.created_at >= since)
    if until is not None:
        stmt = stmt.where(AdminAuditLog.created_at <= until)

    rows = (await db.execute(stmt.order_by(AdminAuditLog.created_at.desc()))).scalars().all()
    total = len(rows)
    start = (page - 1) * page_size
    page_rows = rows[start : start + page_size]

    return Page[AuditLogEntryOut](
        items=[_to_out(r) for r in page_rows], total=total, page=page, page_size=page_size
    )
