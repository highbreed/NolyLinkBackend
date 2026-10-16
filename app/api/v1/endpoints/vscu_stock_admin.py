"""VSCU stock reconciliation admin/diagnostic API — POST /v1/vscu/{branch_id}/stock/
{sar_no}/reconcile. Split out purely to keep each endpoint file a manageable size, same as
vscu_sales_admin.py; mounts under the same /v1/vscu prefix and shares its admin gate.

Exists for the same reason the sales one does: /stock/saveStockItems unconditionally rewrites
its response to resultCd="000" after queuing a local resend on 894/896 (confirmed — see
app/integrations/vscu/schemas/stock.py's module docstring), so a StockIOSubmission row's own
status="SENT" is not proof KRA actually holds that stock movement. This answers "does VSCU
itself hold stock movement sarNo N" directly against /stock/selectStockItems, which — unlike
the save endpoint — is a plain proxy that doesn't rewrite a real rejection.
"""

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_auth import require_admin
from app.db.session import get_db
from app.integrations.vscu.client import VSCUClient
from app.services import vscu_stock_service
from app.services.vscu_service import _get_branch

router = APIRouter()


class StockReconcileResult(BaseModel):
    confirmed: bool
    sar_no: int
    ocrn_dt: str | None = None
    tot_item_cnt: int | None = None
    tot_taxbl_amt: Decimal | None = None
    tot_tax_amt: Decimal | None = None
    tot_amt: Decimal | None = None
    cust_tin: str | None = None
    remark: str | None = None


@router.post("/{branch_id}/stock/{sar_no}/reconcile", response_model=StockReconcileResult)
async def reconcile_stock(
    branch_id: uuid.UUID,
    sar_no: int,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> StockReconcileResult:
    client = VSCUClient(db=db)
    try:
        branch = await _get_branch(db, branch_id)
        matched = await vscu_stock_service.reconcile_by_sar_no(client, branch, sar_no)
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    finally:
        await client.aclose()

    if matched is None:
        return StockReconcileResult(confirmed=False, sar_no=sar_no)
    return StockReconcileResult(
        confirmed=True,
        sar_no=sar_no,
        ocrn_dt=matched.ocrnDt,
        tot_item_cnt=matched.totItemCnt,
        tot_taxbl_amt=matched.totTaxblAmt,
        tot_tax_amt=matched.totTaxAmt,
        tot_amt=matched.totAmt,
        cust_tin=matched.custTin,
        remark=matched.remark,
    )
