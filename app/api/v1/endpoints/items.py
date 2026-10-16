"""POST /v1/items, GET /v1/items, POST /v1/items/{sku}/composition (spec §18).

One request shape (ClientItemIn) regardless of whether the branch is OSCU or VSCU underneath
(Branch.device_type) — same pattern app/api/v1/endpoints/sales.py already established for
sales invoices. The VSCU path (app/services/mapping/vscu_item_mapper.py) is a parallel
mapper, not a refactor of the OSCU one, since the wire DTOs genuinely differ (VSCU's
ItemSaveReq has no cmcKey); both share the same validation/allow-list enforcement
(app/services/mapping/generic_mapper.py) and the same Item table.
"""

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Branch, Client, Item
from app.db.session import get_db
from app.integrations.vscu.client import VSCUClient
from app.integrations.vscu.exceptions import VSCUError
from app.integrations.vscu.schemas.item import ItemCompositionSaveReq
from app.schemas.client.item_in import ClientItemIn
from app.services.client_auth import get_authorized_branch
from app.services.kra_adapter import KraAdapter, KraError
from app.services.mapping.generic_mapper import CodeLookupError, map_client_item_to_kra
from app.services.mapping.vscu_item_mapper import map_client_item_to_vscu
from app.services.rate_limit import enforce_rate_limit

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


def _upsert_item(
    db: AsyncSession,
    branch: Branch,
    existing: Item | None,
    payload: ClientItemIn,
    item_cd: str,
    item_cls_cd: str,
    tax_ty_cd: str,
    pkg_unit_cd: str,
    qty_unit_cd: str,
) -> Item:
    """Shared by both the OSCU and VSCU paths below — their ItemSaveReq schemas use the same
    field names (itemClsCd/taxTyCd/pkgUnitCd/qtyUnitCd) for these resolved values, so the
    caller just passes them through regardless of which mapper produced them.
    """
    now = datetime.now()
    if existing is not None:
        existing.item_cls_cd = item_cls_cd
        existing.item_nm = payload.name
        existing.tax_ty_cd = tax_ty_cd
        existing.dft_prc = payload.unit_price
        existing.pkg_unit_cd = pkg_unit_cd
        existing.qty_unit_cd = qty_unit_cd
        existing.kra_synced_at = now
        return existing

    item = Item(
        branch_id=branch.id,
        sku=payload.sku,
        item_cd=item_cd,
        item_cls_cd=item_cls_cd,
        item_nm=payload.name,
        tax_ty_cd=tax_ty_cd,
        dft_prc=payload.unit_price,
        pkg_unit_cd=pkg_unit_cd,
        qty_unit_cd=qty_unit_cd,
        kra_synced_at=now,
    )
    db.add(item)
    return item


@router.post("", response_model=ItemOut, status_code=201)
async def create_item(
    payload: ClientItemIn,
    client: Client = Depends(enforce_rate_limit),
    db: AsyncSession = Depends(get_db),
) -> ItemOut:
    branch = await get_authorized_branch(db, client, payload.branch_code)

    existing = await db.scalar(
        select(Item).where(Item.branch_id == branch.id, Item.sku == payload.sku)
    )

    if branch.device_type == "VSCU":
        vscu_client = VSCUClient(db=db)
        try:
            try:
                req, item_cd = await map_client_item_to_vscu(
                    db,
                    branch,
                    payload,
                    regr_id="gateway",
                    regr_nm="VSCU Gateway",
                    customer_id=client.customer_id,
                    existing_item_cd=existing.item_cd if existing else None,
                )
            except CodeLookupError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

            try:
                await vscu_client.save_item(branch.id, req)
            except VSCUError as exc:
                raise HTTPException(status_code=502, detail=f"VSCU error: {exc}") from exc

            item = _upsert_item(
                db,
                branch,
                existing,
                payload,
                item_cd,
                req.itemClsCd,
                req.taxTyCd,
                req.pkgUnitCd,
                req.qtyUnitCd,
            )
            await db.commit()
        finally:
            await vscu_client.aclose()
        return _to_item_out(item, branch.branch_code)

    kra = KraAdapter(db=db)
    try:
        try:
            req, item_cd = await map_client_item_to_kra(
                db,
                branch,
                payload,
                regr_id="gateway",
                regr_nm="OSCU Gateway",
                customer_id=client.customer_id,
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

        item = _upsert_item(
            db,
            branch,
            existing,
            payload,
            item_cd,
            req.itemClsCd,
            req.taxTyCd,
            req.pkgUnitCd,
            req.qtyUnitCd,
        )
        await db.commit()
    finally:
        await kra.aclose()

    return _to_item_out(item, branch.branch_code)


@router.get("", response_model=list[ItemOut])
async def list_items(
    branch_code: str | None = None,
    client: Client = Depends(enforce_rate_limit),
    db: AsyncSession = Depends(get_db),
) -> list[ItemOut]:
    stmt = select(Item, Branch).join(Branch, Item.branch_id == Branch.id)
    if branch_code is not None:
        await get_authorized_branch(db, client, branch_code)
        stmt = stmt.where(Branch.branch_code == branch_code)
    else:
        # No branch filter given — scope to what this client's Customer is allowed to see
        # rather than every branch in the gateway (spec §15 branch allow-listing).
        stmt = stmt.where(Branch.id.in_(client.customer.allowed_branch_ids))

    result = await db.execute(stmt)
    return [_to_item_out(item, branch.branch_code) for item, branch in result.all()]


class ItemCompositionIn(BaseModel):
    """No schema exists for OSCU's composition fields (spec §5.1/§20 flags even the endpoint
    URL as unconfirmed) — composition stays an opaque payload passed straight to KRA on that
    path. VSCU's shape, by contrast, IS confirmed (decompiled ItemCompositionSaveReq:
    itemCd/cpstItemCd/cpstQty/regrId/regrNm) — for a VSCU branch, `composition` must carry
    `cpstItemCd` (the composed/component item's KRA code) and `cpstQty` (its quantity); any
    other keys are ignored rather than silently forwarded, since VSCU's local app validates
    against the real DTO shape, unlike OSCU's genuinely-unknown one.
    """

    branch_code: str
    composition: dict[str, Any]


@router.post("/{sku}/composition", status_code=202)
async def save_item_composition(
    sku: str,
    payload: ItemCompositionIn,
    client: Client = Depends(enforce_rate_limit),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    branch = await get_authorized_branch(db, client, payload.branch_code)

    item = await db.scalar(select(Item).where(Item.branch_id == branch.id, Item.sku == sku))
    if item is None:
        raise HTTPException(
            status_code=404, detail=f"No item with sku={sku!r} for branch {payload.branch_code!r}"
        )

    if branch.device_type == "VSCU":
        try:
            cpst_item_cd = str(payload.composition["cpstItemCd"])
            cpst_qty = Decimal(str(payload.composition["cpstQty"]))
        except (KeyError, InvalidOperation) as exc:
            raise HTTPException(
                status_code=422,
                detail=(
                    "VSCU item composition requires numeric 'cpstQty' and a 'cpstItemCd' "
                    "string in composition"
                ),
            ) from exc

        vscu_client = VSCUClient(db=db)
        try:
            req = ItemCompositionSaveReq(
                tin=branch.tin,
                bhfId=branch.bhf_id,
                itemCd=item.item_cd,
                cpstItemCd=cpst_item_cd,
                cpstQty=cpst_qty,
                regrId="gateway",
                regrNm="VSCU Gateway",
            )
            try:
                res = await vscu_client.save_item_composition(branch.id, req)
            except VSCUError as exc:
                raise HTTPException(status_code=502, detail=f"VSCU error: {exc}") from exc
            await db.commit()
        finally:
            await vscu_client.aclose()
        return {"resultCd": res.resultCd, "resultMsg": res.resultMsg}

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
