"""POST /v1/branches/customers, /users, /insurance (spec §18).

These are direct pass-throughs — spec §9.2's crosswalk requirement (friendly enum -> KRA
code) doesn't apply here; every field maps 1:1 onto its KRA counterpart, just wrapped with
branch_code instead of raw tin/bhfId/cmcKey.
"""

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Branch
from app.db.session import get_db
from app.schemas.kra.branch import BhfCustSaveReq, BhfInsuranceSaveReq, BhfUserSaveReq
from app.services import cmc_key_manager
from app.services.kra_adapter import KraAdapter, KraError

router = APIRouter()


async def _get_branch_or_404(db: AsyncSession, branch_code: str) -> Branch:
    try:
        return await cmc_key_manager.get_branch_credentials(db, branch_code)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class BranchCustomerIn(BaseModel):
    branch_code: str
    cust_no: str = Field(max_length=9)
    cust_tin: str = Field(max_length=11)
    cust_nm: str = Field(max_length=60)
    adrs: str | None = None
    tel_no: str | None = None
    email: str | None = None
    fax_no: str | None = None
    remark: str | None = None


@router.post("/customers", status_code=201)
async def save_branch_customer(
    payload: BranchCustomerIn, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    branch = await _get_branch_or_404(db, payload.branch_code)
    req = BhfCustSaveReq(
        tin=branch.tin,
        bhfId=branch.bhf_id,
        cmcKey=branch.cmc_key,
        custNo=payload.cust_no,
        custTin=payload.cust_tin,
        custNm=payload.cust_nm,
        adrs=payload.adrs,
        telNo=payload.tel_no,
        email=payload.email,
        faxNo=payload.fax_no,
        remark=payload.remark,
    )
    kra = KraAdapter(db=db)
    try:
        try:
            res = await kra.save_branch_customer(req, branch_id=branch.id)
        except KraError as exc:
            raise HTTPException(
                status_code=502, detail=f"KRA error {exc.result_cd}: {exc.result_msg}"
            ) from exc
        await db.commit()
    finally:
        await kra.aclose()
    return {"resultCd": res.resultCd, "resultMsg": res.resultMsg}


class BranchUserIn(BaseModel):
    branch_code: str
    user_id: str = Field(max_length=20)
    user_nm: str = Field(max_length=60)
    pwd: str = Field(max_length=255)


@router.post("/users", status_code=201)
async def save_branch_user(
    payload: BranchUserIn, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    branch = await _get_branch_or_404(db, payload.branch_code)
    req = BhfUserSaveReq(
        tin=branch.tin,
        bhfId=branch.bhf_id,
        cmcKey=branch.cmc_key,
        userId=payload.user_id,
        userNm=payload.user_nm,
        pwd=payload.pwd,
    )
    kra = KraAdapter(db=db)
    try:
        try:
            res = await kra.save_branch_user(req, branch_id=branch.id)
        except KraError as exc:
            raise HTTPException(
                status_code=502, detail=f"KRA error {exc.result_cd}: {exc.result_msg}"
            ) from exc
        await db.commit()
    finally:
        await kra.aclose()
    return {"resultCd": res.resultCd, "resultMsg": res.resultMsg}


class BranchInsuranceIn(BaseModel):
    branch_code: str
    isrcc_cd: str = Field(max_length=10)
    isrcc_nm: str = Field(max_length=100)
    isrc_rt: Decimal = Field(max_digits=3)


@router.post("/insurance", status_code=201)
async def save_branch_insurance(
    payload: BranchInsuranceIn, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    branch = await _get_branch_or_404(db, payload.branch_code)
    req = BhfInsuranceSaveReq(
        tin=branch.tin,
        bhfId=branch.bhf_id,
        cmcKey=branch.cmc_key,
        isrccCd=payload.isrcc_cd,
        isrccNm=payload.isrcc_nm,
        isrcRt=payload.isrc_rt,
    )
    kra = KraAdapter(db=db)
    try:
        try:
            res = await kra.save_branch_insurance(req, branch_id=branch.id)
        except KraError as exc:
            raise HTTPException(
                status_code=502, detail=f"KRA error {exc.result_cd}: {exc.result_msg}"
            ) from exc
        await db.commit()
    finally:
        await kra.aclose()
    return {"resultCd": res.resultCd, "resultMsg": res.resultMsg}
