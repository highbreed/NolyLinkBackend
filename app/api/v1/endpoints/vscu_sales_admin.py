"""VSCU sales reconciliation admin/diagnostic API — POST /v1/vscu/{branch_id}/sales/{invc_no}/
reconcile. Split out purely to keep each endpoint file a manageable size; mounts under the
same /v1/vscu prefix and shares its admin gate.

Looking up a SalesInvoice by our own external_ref doesn't need a VSCU-specific endpoint —
GET /v1/sales-invoices/{external_ref} (app/api/v1/endpoints/sales.py) already reads the
shared SalesInvoice table and works for a VSCU branch exactly as it does for OSCU. This
endpoint answers the complementary question ops actually needs when external_ref isn't
enough — "does VSCU itself hold invoice number N as fiscalized" — for the case an attempt's
own SalesInvoice write never landed (e.g. a crash mid-request) so there's no external_ref to
look it up by locally in the first place.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_auth import require_admin
from app.db.session import get_db
from app.integrations.vscu.client import VSCUClient
from app.services.vscu_sales_service import VSCUSalesService
from app.services.vscu_service import _get_branch

router = APIRouter()


class ReconcileResult(BaseModel):
    confirmed: bool
    invc_no: int
    rcpt_no: int | None = None
    tot_rcpt_no: int | None = None
    rcpt_sign: str | None = None
    intrl_data: str | None = None
    sdc_datetime: str | None = None


@router.post("/{branch_id}/sales/{invc_no}/reconcile", response_model=ReconcileResult)
async def reconcile_sale(
    branch_id: uuid.UUID,
    invc_no: int,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> ReconcileResult:
    client = VSCUClient(db=db)
    try:
        branch = await _get_branch(db, branch_id)
        service = VSCUSalesService(db, client)
        result = await service.reconcile_by_invoice_number(branch, invc_no)
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        await client.aclose()

    if result is None or result.data is None:
        return ReconcileResult(confirmed=False, invc_no=invc_no)
    return ReconcileResult(
        confirmed=True,
        invc_no=invc_no,
        rcpt_no=result.data.rcptNo,
        tot_rcpt_no=result.data.totRcptNo,
        rcpt_sign=result.data.rcptSign,
        intrl_data=result.data.intrlData,
        sdc_datetime=result.data.vsdcRcptPbctDate,
    )
