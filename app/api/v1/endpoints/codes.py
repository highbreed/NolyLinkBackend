"""GET /v1/codes/{classification} served from local cache, never a KRA passthrough (spec §18)."""

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Client
from app.db.session import get_db
from app.services import code_cache_service
from app.services.rate_limit import enforce_rate_limit

router = APIRouter()


class CodeOut(BaseModel):
    cd: str
    cd_nm: str | None
    cd_desc: str | None
    srt_ord: int | None
    use_yn: str | None
    refreshed_at: datetime

    model_config = {"from_attributes": True}


@router.get("/{classification}", response_model=list[CodeOut])
async def get_codes(
    classification: str,
    client: Client = Depends(enforce_rate_limit),
    db: AsyncSession = Depends(get_db),
) -> list[CodeOut]:
    rows = await code_cache_service.get_codes(db, classification)
    return [CodeOut.model_validate(row) for row in rows]
