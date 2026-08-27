"""GET /v1/health — liveness/readiness, including the last successful poll timestamp per
resource (Phase 10). Reports the most recent across all branches per resource — a per-branch
breakdown isn't needed for a liveness check and would grow unbounded with the branch count.
"""

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import KraSyncState
from app.db.session import get_db

router = APIRouter(tags=["health"])


class HealthOut(BaseModel):
    status: str
    last_successful_poll: dict[str, datetime]


@router.get("/health", response_model=HealthOut)
async def health(db: AsyncSession = Depends(get_db)) -> HealthOut:
    result = await db.execute(
        select(KraSyncState.resource, func.max(KraSyncState.updated_at)).group_by(
            KraSyncState.resource
        )
    )
    last_successful_poll: dict[str, datetime] = dict(result.all())  # type: ignore[arg-type]
    return HealthOut(status="ok", last_successful_poll=last_successful_poll)
