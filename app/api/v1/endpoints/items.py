"""POST /v1/items, GET /v1/items, POST /v1/items/{sku}/composition (spec §18)."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Branch, Item
from app.db.session import get_db
from app.schemas.client.item_in import ClientItemIn
from app.services import cmc_key_manager
from app.services.kra_adapter import KraAdapter, KraError
from app.services.mapping.generic_mapper import CodeLookupError, map_client_item_to_kra

router = APIRouter()


class ItemOut(BaseModel):
    branch_code: str
    sku: str
    item_cd: str
    item_cls_cd: str
    name: str
    tax_category_cd: str
    unit_price: Decimal
    kra_synced_at: datetime | None


def _to_item_out(item: Item, branch_code: str) -> ItemOut:
    return ItemOut(
        branch_code=branch_code,
        sku=item.sku,
        item_cd=item.item_cd,
        item_cls_cd=item.item_cls_cd,
        name=item.item_nm,
        tax_category_cd=item.tax_ty_cd,
        unit_price=item.dft_prc,
        kra_synced_at=item.kra_synced_at,
    )


@router.post("", response_model=ItemOut, status_code=201)
async def create_item(
    payload: ClientItemIn,
    db: AsyncSession = Depends(get_db),
) -> ItemOut:
    try:
        branch = await cmc_key_manager.get_branch_credentials(db, payload.branch_code)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    existing = await db.scalar(
        select(Item).where(Item.branch_id == branch.id, Item.sku == payload.sku)
    )

    kra = KraAdapter(db=db)
    try:
        try:
            req, item_cd = await map_client_item_to_kra(
                db,
                branch,
                payload,
                regr_id="gateway",
                regr_nm="OSCU Gateway",
                existing_item_cd=existing.item_cd if existing else None,
            )
        except CodeLookupError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        try:
            await kra.save_item(req, branch_id=branch.id)
        except KraError as exc:
            raise HTTPException(
                status_code=502, detail=f"KRA error {exc.result_cd}: {exc.result_msg}"
            ) from exc

        now = datetime.now()
        if existing is not None:
            existing.item_cls_cd = payload.item_classification_cd
            existing.item_nm = payload.name
            existing.tax_ty_cd = req.taxTyCd
            existing.dft_prc = payload.unit_price
            existing.kra_synced_at = now
            item = existing
        else:
            item = Item(
                branch_id=branch.id,
                sku=payload.sku,
                item_cd=item_cd,
                item_cls_cd=payload.item_classification_cd,
                item_nm=payload.name,
                tax_ty_cd=req.taxTyCd,
                dft_prc=payload.unit_price,
                kra_synced_at=now,
            )
            db.add(item)
        await db.commit()
    finally:
        await kra.aclose()

    return _to_item_out(item, branch.branch_code)


@router.get("", response_model=list[ItemOut])
async def list_items(
    branch_code: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[ItemOut]:
    stmt = select(Item, Branch).join(Branch, Item.branch_id == Branch.id)
    if branch_code is not None:
        stmt = stmt.where(Branch.branch_code == branch_code)

    result = await db.execute(stmt)
    return [_to_item_out(item, branch.branch_code) for item, branch in result.all()]


class ItemCompositionIn(BaseModel):
    """No schema exists for composition's fields (spec §5.1/§20 flags even the endpoint URL
    as unconfirmed) — composition is an opaque payload passed straight to KRA.
    """

    branch_code: str
    composition: dict[str, Any]


@router.post("/{sku}/composition", status_code=202)
async def save_item_composition(
    sku: str,
    payload: ItemCompositionIn,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        branch = await cmc_key_manager.get_branch_credentials(db, payload.branch_code)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    item = await db.scalar(select(Item).where(Item.branch_id == branch.id, Item.sku == sku))
    if item is None:
        raise HTTPException(
            status_code=404, detail=f"No item with sku={sku!r} for branch {payload.branch_code!r}"
        )

    kra = KraAdapter(db=db)
    try:
        body = {
            **payload.composition,
            "tin": branch.tin,
            "bhfId": branch.bhf_id,
            "cmcKey": branch.cmc_key,
            "itemCd": item.item_cd,
        }
        try:
            res = await kra.save_item_composition(body, branch_id=branch.id)
        except KraError as exc:
            raise HTTPException(
                status_code=502, detail=f"KRA error {exc.result_cd}: {exc.result_msg}"
            ) from exc
        await db.commit()
    finally:
        await kra.aclose()

    return {"resultCd": res.resultCd, "resultMsg": res.resultMsg}
