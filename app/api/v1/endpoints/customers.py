"""Admin-only customer onboarding: create a Customer (the paying business) and the Client
connector credential(s) that integrate on its behalf — see Customer/Client docstrings in
app/db/models.py. Replaces the manual raw-SQL insert docs/client_onboarding_and_monitoring.md
previously documented as the only way to create a new tenant.

Gated the same way as billing.py/device.py — X-Admin-Api-Key or a portal admin JWT, never
reachable by a Client's own OAuth2 token.
"""

import logging
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_auth import require_admin
from app.db.models import Branch, Client, Customer
from app.db.session import get_db
from app.services import admin_audit, customer_service
from app.services.customer_onboarding_service import BranchRegistrationInput, onboard_customer

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_admin)])


async def _branch_codes_for(db: AsyncSession, branch_ids: list[uuid.UUID]) -> list[str]:
    if not branch_ids:
        return []
    result = await db.execute(select(Branch.branch_code).where(Branch.id.in_(branch_ids)))
    return sorted(result.scalars().all())


class CustomerCreateRequest(BaseModel):
    name: str
    branch_codes: list[str] = []


class CustomerUpdateRequest(BaseModel):
    name: str | None = None
    branch_codes: list[str] | None = None


class CustomerOut(BaseModel):
    id: uuid.UUID
    name: str
    branch_codes: list[str]
    created_at: datetime


async def _to_customer_out(db: AsyncSession, customer: Customer) -> CustomerOut:
    return CustomerOut(
        id=customer.id,
        name=customer.name,
        branch_codes=await _branch_codes_for(db, customer.allowed_branch_ids),
        created_at=customer.created_at,
    )


@router.post("", response_model=CustomerOut, status_code=201)
async def create_customer(
    payload: CustomerCreateRequest,
    request: Request,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> CustomerOut:
    try:
        branch_ids = await customer_service.resolve_branch_codes(db, payload.branch_codes)
    except customer_service.UnknownBranchCodesError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    customer = await customer_service.create_customer(db, payload.name, branch_ids)
    await admin_audit.record_admin_action(
        db,
        actor=admin_actor,
        action="customer.create",
        target_type="customer",
        target_id=str(customer.id),
        after={"name": customer.name, "branch_codes": payload.branch_codes},
        request=request,
    )
    await db.commit()
    logger.info("Customer %s (%s) created by %s", customer.id, customer.name, admin_actor)
    return await _to_customer_out(db, customer)


@router.get("", response_model=list[CustomerOut])
async def list_customers(db: AsyncSession = Depends(get_db)) -> list[CustomerOut]:
    customers = await customer_service.list_customers(db)
    return [await _to_customer_out(db, c) for c in customers]


@router.get("/{customer_id}", response_model=CustomerOut)
async def get_customer(customer_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> CustomerOut:
    customer = await customer_service.get_customer(db, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail="No customer with that id")
    return await _to_customer_out(db, customer)


@router.patch("/{customer_id}", response_model=CustomerOut)
async def update_customer(
    customer_id: uuid.UUID,
    payload: CustomerUpdateRequest,
    request: Request,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> CustomerOut:
    branch_ids: list[uuid.UUID] | None = None
    if payload.branch_codes is not None:
        try:
            branch_ids = await customer_service.resolve_branch_codes(db, payload.branch_codes)
        except customer_service.UnknownBranchCodesError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        customer = await customer_service.update_customer(db, customer_id, payload.name, branch_ids)
        await admin_audit.record_admin_action(
            db,
            actor=admin_actor,
            action="customer.update",
            target_type="customer",
            target_id=str(customer_id),
            after={"name": payload.name, "branch_codes": payload.branch_codes},
            request=request,
        )
        await db.commit()
    except customer_service.CustomerNotFoundError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    logger.info("Customer %s updated by %s", customer_id, admin_actor)
    return await _to_customer_out(db, customer)


class BranchOnboardIn(BaseModel):
    branch_code: str
    tin: str
    bhf_id: str
    device_type: Literal["OSCU", "VSCU"] = "OSCU"
    # Required for device_type="VSCU" — the customer's local VSCU app URL on their own
    # network, e.g. "http://192.168.1.10:8088". Not the KRA-issued dvcSrlNo: that's a single
    # value KRA issues domain-wide to this integration, not per branch/customer, and is
    # resolved automatically from Settings.vscu_dvc_srl_no_for_env() — see that setting's
    # docstring in app/core/config.py.
    base_url: str | None = None
    kra_base_url_override: str | None = None

    @model_validator(mode="after")
    def _base_url_required_for_vscu(self) -> "BranchOnboardIn":
        if self.device_type == "VSCU" and not self.base_url:
            raise ValueError(f"branch {self.branch_code!r}: base_url is required for VSCU")
        return self


class CustomerOnboardRequest(BaseModel):
    name: str
    branches: list[BranchOnboardIn]


class BranchOnboardResult(BaseModel):
    branch_code: str
    device_type: str
    status: Literal["REGISTERED", "FAILED"]
    branch_id: uuid.UUID | None
    error: str | None


class CustomerOnboardResponse(BaseModel):
    customer: CustomerOut
    branches: list[BranchOnboardResult]


@router.post("/onboard", response_model=CustomerOnboardResponse, status_code=201)
async def onboard(
    payload: CustomerOnboardRequest,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> CustomerOnboardResponse:
    """One call for a stepper-form onboarding screen: registers every branch (KRA-side device
    registration, plus local VSCU app initialization for VSCU ones) and creates the Customer
    with whichever branches succeeded — see app/services/customer_onboarding_service.py's
    module docstring for why this is best-effort per branch rather than all-or-nothing (KRA
    registration can't be undone, so a real rollback across branches would be actively unsafe).

    Always returns 201 with a `branches` status array, even if every branch failed (the
    Customer is still created, with no branch access yet — fix the failed branches and retry;
    resubmitting is safe, KRA/VSCU both treat "already registered" as success on a retry).
    """
    branch_inputs = [
        BranchRegistrationInput(
            branch_code=b.branch_code,
            tin=b.tin,
            bhf_id=b.bhf_id,
            device_type=b.device_type,
            base_url=b.base_url,
            kra_base_url_override=b.kra_base_url_override,
        )
        for b in payload.branches
    ]
    customer, outcomes = await onboard_customer(db, payload.name, branch_inputs)

    logger.info(
        "Customer %s (%s) onboarded by %s: %d/%d branches registered",
        customer.id,
        customer.name,
        admin_actor,
        sum(1 for o in outcomes if o.status == "REGISTERED"),
        len(outcomes),
    )
    return CustomerOnboardResponse(
        customer=await _to_customer_out(db, customer),
        branches=[
            BranchOnboardResult(
                branch_code=o.branch_code,
                device_type=o.device_type,
                status=o.status,
                branch_id=o.branch_id,
                error=o.error,
            )
            for o in outcomes
        ],
    )


class ConnectorCreateRequest(BaseModel):
    name: str


class ConnectorCreateResponse(BaseModel):
    """client_secret is plaintext and shown exactly once — it can't be recovered later, only
    re-issued via POST .../rotate (same client_id, a fresh secret).
    """

    client_id: uuid.UUID
    client_secret: str
    name: str


class ConnectorOut(BaseModel):
    client_id: uuid.UUID
    name: str
    created_at: datetime
    revoked_at: datetime | None


def _to_connector_out(client: Client) -> ConnectorOut:
    return ConnectorOut(
        client_id=client.id,
        name=client.name,
        created_at=client.created_at,
        revoked_at=client.revoked_at,
    )


@router.post("/{customer_id}/connectors", response_model=ConnectorCreateResponse, status_code=201)
async def create_connector(
    customer_id: uuid.UUID,
    payload: ConnectorCreateRequest,
    request: Request,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> ConnectorCreateResponse:
    try:
        client, raw_secret = await customer_service.create_connector(db, customer_id, payload.name)
        # Never the secret itself — this would defeat the "shown once" model (SecretOncePanel).
        await admin_audit.record_admin_action(
            db,
            actor=admin_actor,
            action="connector.create",
            target_type="connector",
            target_id=str(client.id),
            after={"name": client.name, "customer_id": str(customer_id)},
            request=request,
        )
        await db.commit()
    except customer_service.CustomerNotFoundError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    logger.info(
        "Connector %s (%s) created for customer_id=%s by %s",
        client.id,
        client.name,
        customer_id,
        admin_actor,
    )
    return ConnectorCreateResponse(client_id=client.id, client_secret=raw_secret, name=client.name)


@router.get("/{customer_id}/connectors", response_model=list[ConnectorOut])
async def list_connectors(
    customer_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> list[ConnectorOut]:
    clients = await customer_service.list_connectors(db, customer_id)
    return [_to_connector_out(c) for c in clients]


@router.post("/{customer_id}/connectors/{client_id}/revoke", response_model=ConnectorOut)
async def revoke_connector(
    customer_id: uuid.UUID,
    client_id: uuid.UUID,
    request: Request,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> ConnectorOut:
    """Soft-revokes the connector — its existing OAuth2 tokens stop working immediately
    (app/services/client_auth.py), but the row and its fiscal/idempotency history stay intact.
    Idempotent: revoking an already-revoked connector just returns its current state.
    """
    try:
        client = await customer_service.revoke_connector(db, customer_id, client_id)
        await admin_audit.record_admin_action(
            db,
            actor=admin_actor,
            action="connector.revoke",
            target_type="connector",
            target_id=str(client_id),
            before={"revoked_at": None},
            after={"revoked_at": client.revoked_at.isoformat() if client.revoked_at else None},
            request=request,
        )
        await db.commit()
    except customer_service.ConnectorNotFoundError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    logger.info(
        "Connector %s revoked for customer_id=%s by %s", client_id, customer_id, admin_actor
    )
    return _to_connector_out(client)


@router.post("/{customer_id}/connectors/{client_id}/rotate", response_model=ConnectorCreateResponse)
async def rotate_connector(
    customer_id: uuid.UUID,
    client_id: uuid.UUID,
    request: Request,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> ConnectorCreateResponse:
    """Issues a new client_secret for the same client_id — the integrator updates only the
    secret it holds, not its client_id. Every previously-issued token for this connector
    becomes invalid the moment its old secret stops matching (a new POST /oauth/token with the
    old secret fails with 401); tokens already handed out before rotation remain valid until
    they naturally expire, same as today.
    """
    try:
        client, raw_secret = await customer_service.rotate_connector_secret(
            db, customer_id, client_id
        )
        # Never the secret itself — see create_connector's own note above.
        await admin_audit.record_admin_action(
            db,
            actor=admin_actor,
            action="connector.rotate",
            target_type="connector",
            target_id=str(client_id),
            request=request,
        )
        await db.commit()
    except customer_service.ConnectorNotFoundError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    logger.info(
        "Connector %s secret rotated for customer_id=%s by %s", client_id, customer_id, admin_actor
    )
    return ConnectorCreateResponse(client_id=client.id, client_secret=raw_secret, name=client.name)


class AllowlistOut(BaseModel):
    classification: str
    codes: list[str]


class AllowlistSetRequest(BaseModel):
    codes: list[str]


@router.get("/{customer_id}/code-allowlist", response_model=list[AllowlistOut])
async def get_code_allowlist(
    customer_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> list[AllowlistOut]:
    """Every Client under this Customer shares one allow-list per KRA reference-code
    classification (tax-type/packaging-unit/quantity-unit/nation/item-classification) — see
    CustomerCodeAllowlist's docstring in app/db/models.py. A classification with no entry here
    is unrestricted, not "no codes allowed".
    """
    try:
        grouped = await customer_service.get_code_allowlist(db, customer_id)
    except customer_service.CustomerNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return [AllowlistOut(classification=k, codes=v) for k, v in grouped.items()]


@router.put("/{customer_id}/code-allowlist/{classification}", response_model=AllowlistOut)
async def set_code_allowlist(
    customer_id: uuid.UUID,
    classification: str,
    payload: AllowlistSetRequest,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AllowlistOut:
    """Replaces the full allowed-codes set for this (customer, classification) pair. An empty
    `codes` list clears the restriction — the classification goes back to unrestricted.
    """
    try:
        await customer_service.set_code_allowlist(db, customer_id, classification, payload.codes)
        await db.commit()
    except customer_service.CustomerNotFoundError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    logger.info(
        "Customer %s code-allowlist[%s] set to %d code(s) by %s",
        customer_id,
        classification,
        len(payload.codes),
        admin_actor,
    )
    return AllowlistOut(classification=classification, codes=payload.codes)
