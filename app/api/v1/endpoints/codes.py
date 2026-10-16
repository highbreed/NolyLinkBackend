"""GET /v1/codes/{classification} served from local cache, never a KRA passthrough (spec §18).

Filtered to the calling Customer's configured code allow-list (app/services/
code_allowlist_service.py), if one exists for this classification — a restricted customer
should see the same subset here that it's actually permitted to submit, not the full cache
with no way to know which codes will be rejected until it tries one. Unrestricted customers
(the default — no allow-list rows configured) see everything, same as before.
"""

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Client
from app.db.session import get_db
from app.services import code_allowlist_service, code_cache_service
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
    allowed = await code_allowlist_service.get_allowed_codes(
        db, client.customer_id, code_cache_service.canonical_classification_key(classification)
    )
    if allowed is not None:
        rows = [row for row in rows if row.cd in allowed]
    return [CodeOut.model_validate(row) for row in rows]
