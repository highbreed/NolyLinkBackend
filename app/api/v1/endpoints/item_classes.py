"""GET /v1/item-classes served from local cache, never a KRA passthrough — client-facing
counterpart to app/api/v1/endpoints/codes.py, for the one classification (item classes) that
isn't shaped like a KraCodeList row. See app/services/item_class_cache_service.py.

Filtered to the calling Customer's configured code allow-list (app/services/
code_allowlist_service.py), if one exists for the "item-classification" pseudo-slug — same
reasoning as codes.py: a restricted customer should see only what it can actually submit.
"""

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Client
from app.db.session import get_db
from app.services import code_allowlist_service, item_class_cache_service
from app.services.mapping.generic_mapper import ITEM_CLASSIFICATION
from app.services.rate_limit import enforce_rate_limit

router = APIRouter()


class ItemClassOut(BaseModel):
    item_cls_cd: str
    item_cls_nm: str | None
    item_cls_lvl: str | None
    tax_ty_cd: str | None
    mjr_tg_yn: str | None
    use_yn: str | None
    refreshed_at: datetime

    model_config = {"from_attributes": True}


@router.get("", response_model=list[ItemClassOut])
async def get_item_classes(
    client: Client = Depends(enforce_rate_limit),
    db: AsyncSession = Depends(get_db),
) -> list[ItemClassOut]:
    rows = await item_class_cache_service.get_item_classes(db)
    allowed = await code_allowlist_service.get_allowed_codes(
        db, client.customer_id, ITEM_CLASSIFICATION
    )
    if allowed is not None:
        rows = [row for row in rows if row.item_cls_cd in allowed]
    return [ItemClassOut.model_validate(row) for row in rows]
