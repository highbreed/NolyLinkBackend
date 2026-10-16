"""VSCU reference-data admin/diagnostic API — codes/item-classes/customers/branches/items/
notices/import-items, layered on the VSCU integration layer (app/integrations/vscu/,
app/services/vscu_reference_data_service.py). Split from app/api/v1/endpoints/vscu.py (which
owns init/health/sequences/echo) purely to keep each file a manageable size; both mount under
the same /v1/vscu prefix and share its admin-only gate.

These are admin/diagnostic endpoints, not the client-facing contract SAP/ERP integrations use
(app/api/v1/endpoints/codes.py, branches.py, notices.py, imports.py) — those remain OSCU-only
for now. items.py is the exception: it now dispatches by Branch.device_type (2026-10-14), so
item registration/composition work identically for both protocols there; this module's own
save_item/save_item_composition stay as admin-only diagnostic equivalents, not a client path.
"""

import logging
import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_auth import require_admin
from app.core.config import get_settings
from app.db.session import get_db
from app.integrations.vscu.client import VSCUClient
from app.integrations.vscu.exceptions import VSCUError
from app.integrations.vscu.schemas.import_item import ImportItemUpdateReq
from app.integrations.vscu.schemas.item import ItemSaveReq
from app.services.vscu_purchase_service import sync_purchase_sales
from app.services.vscu_reference_data_service import VSCUReferenceDataService
from app.services.vscu_service import _get_branch
from app.services.vscu_stock_service import sync_stock_moves

logger = logging.getLogger(__name__)

router = APIRouter()


def _client(db: AsyncSession) -> VSCUClient:
    return VSCUClient(db=db)


def _http_error(exc: VSCUError) -> HTTPException:
    # str(exc), not exc.context.result_cd/result_msg: VSCUConfigurationError/VSCUTransportError
    # never carry those (no request was ever attempted, or none ever got a VSCU envelope back).
    return HTTPException(status_code=502, detail=f"VSCU error: {exc}")


class SyncResult(BaseModel):
    synced: bool


@router.post("/{branch_id}/codes/sync", response_model=SyncResult)
async def sync_codes(
    branch_id: uuid.UUID,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> SyncResult:
    client = _client(db)
    try:
        branch = await _get_branch(db, branch_id)
        service = VSCUReferenceDataService(db, client)
        await service.sync_codes(branch)
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except VSCUError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    finally:
        await client.aclose()
    return SyncResult(synced=True)


@router.post("/{branch_id}/notices/sync", response_model=SyncResult)
async def sync_notices(
    branch_id: uuid.UUID,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> SyncResult:
    client = _client(db)
    try:
        branch = await _get_branch(db, branch_id)
        service = VSCUReferenceDataService(db, client)
        await service.sync_notices(branch)
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except VSCUError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    finally:
        await client.aclose()
    return SyncResult(synced=True)


@router.post("/{branch_id}/imports/sync", response_model=SyncResult)
async def sync_import_items(
    branch_id: uuid.UUID,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> SyncResult:
    client = _client(db)
    try:
        branch = await _get_branch(db, branch_id)
        service = VSCUReferenceDataService(db, client)
        await service.sync_import_items(branch)
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except VSCUError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    finally:
        await client.aclose()
    return SyncResult(synced=True)


@router.post("/{branch_id}/purchases/sync", response_model=SyncResult)
async def sync_purchases(
    branch_id: uuid.UUID,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> SyncResult:
    client = _client(db)
    try:
        branch = await _get_branch(db, branch_id)
        await sync_purchase_sales(db, client, branch)
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except VSCUError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    finally:
        await client.aclose()
    return SyncResult(synced=True)


@router.post("/{branch_id}/stock/sync", response_model=SyncResult)
async def sync_stock(
    branch_id: uuid.UUID,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> SyncResult:
    client = _client(db)
    try:
        branch = await _get_branch(db, branch_id)
        await sync_stock_moves(db, client, branch)
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except VSCUError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    finally:
        await client.aclose()
    return SyncResult(synced=True)


class ImportItemDecisionRequest(BaseModel):
    task_cd: str
    dcl_de: str
    item_seq: int
    hs_cd: str
    item_cls_cd: str
    item_cd: str
    impt_item_stts_cd: str
    remark: str | None = None


@router.post("/{branch_id}/imports/update")
async def update_import_item(
    branch_id: uuid.UUID,
    payload: ImportItemDecisionRequest,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str | None]:
    client = _client(db)
    try:
        branch = await _get_branch(db, branch_id)
        req = ImportItemUpdateReq(
            tin=branch.tin,
            bhfId=branch.bhf_id,
            taskCd=payload.task_cd,
            dclDe=payload.dcl_de,
            itemSeq=payload.item_seq,
            hsCd=payload.hs_cd,
            itemClsCd=payload.item_cls_cd,
            itemCd=payload.item_cd,
            imptItemSttsCd=payload.impt_item_stts_cd,
            remark=payload.remark,
            modrId=admin_actor,
            modrNm=admin_actor,
        )
        service = VSCUReferenceDataService(db, client)
        res = await service.update_import_item(branch, req)
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except VSCUError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    finally:
        await client.aclose()
    return {"result_cd": res.resultCd, "result_msg": res.resultMsg}


@router.get("/{branch_id}/item-classes")
async def get_item_classes(
    branch_id: uuid.UUID,
    last_req_dt: str | None = Query(default=None, alias="lastReqDt"),
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    client = _client(db)
    try:
        branch = await _get_branch(db, branch_id)
        service = VSCUReferenceDataService(db, client)
        res = await service.select_item_classes(
            branch, last_req_dt=last_req_dt or get_settings().kra_sync_initial_last_req_dt
        )
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except VSCUError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    finally:
        await client.aclose()
    return res.model_dump(mode="json")


@router.get("/{branch_id}/customers/{cust_tin}")
async def lookup_customer(
    branch_id: uuid.UUID,
    cust_tin: str,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    client = _client(db)
    try:
        branch = await _get_branch(db, branch_id)
        service = VSCUReferenceDataService(db, client)
        res = await service.lookup_customer(branch, cust_tin=cust_tin)
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except VSCUError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    finally:
        await client.aclose()
    return res.model_dump(mode="json")


@router.get("/{branch_id}/branches")
async def get_branches(
    branch_id: uuid.UUID,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    client = _client(db)
    try:
        branch = await _get_branch(db, branch_id)
        service = VSCUReferenceDataService(db, client)
        res = await service.select_branches(
            branch, last_req_dt=get_settings().kra_sync_initial_last_req_dt
        )
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except VSCUError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    finally:
        await client.aclose()
    return res.model_dump(mode="json")


@router.get("/{branch_id}/items")
async def get_items(
    branch_id: uuid.UUID,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    client = _client(db)
    try:
        branch = await _get_branch(db, branch_id)
        service = VSCUReferenceDataService(db, client)
        res = await service.select_items(
            branch, last_req_dt=get_settings().kra_sync_initial_last_req_dt
        )
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except VSCUError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    finally:
        await client.aclose()
    return res.model_dump(mode="json")


class ItemSaveRequest(BaseModel):
    item_cd: str
    item_cls_cd: str
    item_ty_cd: str
    item_nm: str
    item_std_nm: str | None = None
    orgn_nat_cd: str
    pkg_unit_cd: str
    qty_unit_cd: str
    tax_ty_cd: str
    btch_no: str | None = None
    bcd: str | None = None
    dft_prc: Decimal
    add_info: str | None = None
    safety_qty: Decimal | None = None
    insurance_applicable_yn: str | None = None
    use_yn: str | None = "Y"


@router.post("/{branch_id}/items")
async def save_item(
    branch_id: uuid.UUID,
    payload: ItemSaveRequest,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """See app/integrations/vscu/schemas/item.py — resultCd=="000" here does not prove real
    KRA accepted the item, only that the local VSCU app queued the request.
    """
    client = _client(db)
    try:
        branch = await _get_branch(db, branch_id)
        req = ItemSaveReq(
            tin=branch.tin,
            bhfId=branch.bhf_id,
            itemCd=payload.item_cd,
            itemClsCd=payload.item_cls_cd,
            itemTyCd=payload.item_ty_cd,
            itemNm=payload.item_nm,
            itemStdNm=payload.item_std_nm,
            orgnNatCd=payload.orgn_nat_cd,
            pkgUnitCd=payload.pkg_unit_cd,
            qtyUnitCd=payload.qty_unit_cd,
            taxTyCd=payload.tax_ty_cd,
            btchNo=payload.btch_no,
            bcd=payload.bcd,
            dftPrc=payload.dft_prc,
            addInfo=payload.add_info,
            sftyQty=payload.safety_qty,
            isrcAplcbYn=payload.insurance_applicable_yn,
            useYn=payload.use_yn,
            regrId=admin_actor,
            regrNm=admin_actor,
            modrId=admin_actor,
            modrNm=admin_actor,
        )
        service = VSCUReferenceDataService(db, client)
        res = await service.save_item(branch, req)
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except VSCUError as exc:
        await db.rollback()
        raise _http_error(exc) from exc
    finally:
        await client.aclose()
    return {"result_cd": res.resultCd, "result_msg": res.resultMsg}


class ItemReconcileResult(BaseModel):
    confirmed: bool
    item_cd: str
    item_nm: str | None = None
    item_cls_cd: str | None = None
    tax_ty_cd: str | None = None
    dft_prc: Decimal | None = None
    use_yn: str | None = None
    # Whether KRA modified our submitted classification code — see OSCU's ItemListEntry
    # docstring (app/schemas/kra/item.py), same real field, same meaning on the VSCU side.
    rra_mod_yn: str | None = None


@router.post("/{branch_id}/items/{item_cd}/reconcile", response_model=ItemReconcileResult)
async def reconcile_item(
    branch_id: uuid.UUID,
    item_cd: str,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> ItemReconcileResult:
    """Confirms whether VSCU actually holds item_cd via /items/selectItems — a trusted plain
    proxy, unlike /items/saveItems's confirmed 894/896 local-resend quirk (save_item above).
    Exists for the same reason POST /v1/vscu/{branch_id}/stock/{sar_no}/reconcile does: a
    201 from POST /v1/items is not proof KRA has the item.
    """
    client = _client(db)
    try:
        branch = await _get_branch(db, branch_id)
        service = VSCUReferenceDataService(db, client)
        matched = await service.reconcile_item_by_cd(branch, item_cd)
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        await client.aclose()

    if matched is None:
        return ItemReconcileResult(confirmed=False, item_cd=item_cd)
    return ItemReconcileResult(
        confirmed=True,
        item_cd=item_cd,
        item_nm=matched.itemNm,
        item_cls_cd=matched.itemClsCd,
        tax_ty_cd=matched.taxTyCd,
        dft_prc=matched.dftPrc,
        use_yn=matched.useYn,
        rra_mod_yn=matched.rraModYn,
    )
