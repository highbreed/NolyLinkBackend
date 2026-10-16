"""Admin-only read surface over the cached KRA reference-code tables (kra_code_lists,
kra_item_classes) — lets the admin portal browse what's actually available (tax-type,
packaging-unit, quantity-unit, nation, item-classification, ...) before assigning a subset of
it to a Customer via PUT /v1/customers/{id}/code-allowlist/{classification}
(app/api/v1/endpoints/customers.py).

Deliberately separate from app/api/v1/endpoints/codes.py and item_classes.py: those are
client-facing (require a Client's own OAuth2 token, so an admin operator has no way to call
them) and serve a full, unfiltered listing; these mirror them read-for-read over the same
service functions, admin-gated, with the extra q/level/prefix filters an admin UI needs when
browsing ~1000 item-classification rows.
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_auth import require_admin
from app.db.session import get_db
from app.services import code_cache_service, item_class_cache_service

router = APIRouter(dependencies=[Depends(require_admin)])


class ClassificationSummaryOut(BaseModel):
    classification: str
    """The friendly slug (e.g. "tax-type") when code_cache_service.CLASSIFICATION_SLUGS knows
    one for this cd_cls, otherwise the raw cd_cls itself — either value works as the
    `classification` path segment on the endpoints below and on the code-allowlist endpoints.
    """
    cd_cls: str
    cd_cls_nm: str | None
    """KRA's own real name for this classification (e.g. "Bank Type"), captured from
    cdClsNm on sync — present even when `classification` above had to fall back to the raw
    cd_cls (i.e. CLASSIFICATION_SLUGS has no entry for it). None until the first sync after
    this field was added (2026-09-18) has run for a given cd_cls.
    """
    code_count: int


@router.get("/codes", response_model=list[ClassificationSummaryOut])
async def list_classifications(
    db: AsyncSession = Depends(get_db),
) -> list[ClassificationSummaryOut]:
    """Index of every classification actually present in the cache, with a row count — the
    admin UI's entry point for "what can I assign codes from".
    """
    summary = await code_cache_service.get_classification_summary(db)
    return [
        ClassificationSummaryOut(
            classification=row.slug or row.cd_cls,
            cd_cls=row.cd_cls,
            cd_cls_nm=row.cd_cls_nm,
            code_count=row.count,
        )
        for row in summary
    ]


class CodeOut(BaseModel):
    cd: str
    cd_nm: str | None
    cd_desc: str | None
    srt_ord: int | None
    use_yn: str | None

    model_config = {"from_attributes": True}


@router.get("/codes/{classification}", response_model=list[CodeOut])
async def list_codes(
    classification: str,
    q: str | None = Query(default=None, description="Case-insensitive substring match on cd/cd_nm"),
    db: AsyncSession = Depends(get_db),
) -> list[CodeOut]:
    """`classification` accepts either a friendly slug or a raw cdCls — same as
    GET /v1/codes/{classification}, just admin-gated instead of client-token-gated.
    """
    rows = await code_cache_service.get_codes(db, classification, q=q)
    return [CodeOut.model_validate(row) for row in rows]


class ItemClassOut(BaseModel):
    item_cls_cd: str
    item_cls_nm: str | None
    item_cls_lvl: str | None
    tax_ty_cd: str | None
    mjr_tg_yn: str | None
    use_yn: str | None

    model_config = {"from_attributes": True}


@router.get("/item-classes", response_model=list[ItemClassOut])
async def list_item_classes(
    level: str | None = Query(
        default=None, description='"1" (segment) / "2" (family) / "3" (class)'
    ),
    prefix: str | None = Query(
        default=None, description='item_cls_cd prefix, e.g. "50" for Food and Beverage Products'
    ),
    q: str | None = Query(
        default=None, description="Case-insensitive substring match on item_cls_nm"
    ),
    db: AsyncSession = Depends(get_db),
) -> list[ItemClassOut]:
    rows = await item_class_cache_service.get_item_classes(db, level=level, prefix=prefix, q=q)
    return [ItemClassOut.model_validate(row) for row in rows]
