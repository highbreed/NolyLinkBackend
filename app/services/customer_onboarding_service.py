"""Combined customer onboarding (2026-09-01): a single-screen/stepper-form flow that registers
every branch a new Customer needs — KRA-side device registration for every branch, plus local
VSCU app initialization for VSCU ones — and creates the Customer, in one call.

Best-effort, not all-or-nothing. KRA device registration has no "undo" (no delete API), so a
true atomic rollback across multiple branches would be actively dangerous: a branch that
registered successfully before a later branch failed would leave KRA thinking it's installed
with no local record — exactly the DeviceAlreadyProvisionedButUnknown trap
app/services/cmc_key_manager.py already escalates to ops for the single-branch flow. So each
branch is committed independently as it succeeds, and the Customer is created afterward with
whatever branches succeeded (possibly zero) — a partial failure never loses completed work.
Resubmitting the same payload after fixing a failed branch is naturally safe to retry: KRA's
own 902 ("already installed") and VSCUInitializationService.initialize's upsert both already
treat "already registered" as success, not an error, for the branches that already went through.
"""

import logging
import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Customer
from app.integrations.vscu.client import VSCUClient
from app.integrations.vscu.exceptions import VSCUError
from app.services import cmc_key_manager, customer_service
from app.services.kra_adapter import KraAdapter, KraError
from app.services.vscu_service import VSCUInitializationService

logger = logging.getLogger(__name__)


@dataclass
class BranchRegistrationInput:
    branch_code: str
    tin: str
    bhf_id: str
    device_type: str
    # Required for device_type="VSCU" (the customer's local VSCU app URL); unused for OSCU.
    # Validated by the request schema, not re-checked here.
    base_url: str | None = None
    kra_base_url_override: str | None = None


@dataclass
class BranchRegistrationOutcome:
    branch_code: str
    device_type: str
    status: Literal["REGISTERED", "FAILED"]
    branch_id: uuid.UUID | None
    error: str | None


def _failure(branch_in: BranchRegistrationInput, error: str) -> BranchRegistrationOutcome:
    return BranchRegistrationOutcome(
        branch_code=branch_in.branch_code,
        device_type=branch_in.device_type,
        status="FAILED",
        branch_id=None,
        error=error,
    )


async def _owning_customer(db: AsyncSession, branch_id: uuid.UUID) -> Customer | None:
    """Guards against a duplicate/mistyped (tin, bhfId) silently handing one customer access
    to another customer's branch. cmc_key_manager.initialize_device's existing "already
    installed" (902) fallback returns whatever Branch row already exists for that (tin, bhfId)
    regardless of who it belongs to — Branch itself has no owner column, only
    Customer.allowed_branch_ids does. Without this check, a data-entry mistake during
    onboarding could quietly grant a new customer access to an existing one's fiscal branch.

    O(customers) — a full table scan in Python, not indexed. Fine at today's scale (this only
    runs once per onboarding call, admin-triggered, not a hot path); revisit with a reverse
    index if the customer count ever makes that matter.
    """
    result = await db.execute(select(Customer))
    for customer in result.scalars().all():
        if branch_id in customer.allowed_branch_ids:
            return customer
    return None


async def register_branch(
    db: AsyncSession, kra: KraAdapter, branch_in: BranchRegistrationInput
) -> BranchRegistrationOutcome:
    try:
        branch = await cmc_key_manager.initialize_device(
            db,
            kra,
            branch_in.branch_code,
            branch_in.tin,
            branch_in.bhf_id,
            device_type=branch_in.device_type,
            kra_base_url_override=branch_in.kra_base_url_override,
        )

        owner = await _owning_customer(db, branch.id)
        if owner is not None:
            # Read owner.name BEFORE rollback: rollback() expires every object in the
            # session's identity map regardless of expire_on_commit, so accessing it after
            # would trigger an implicit lazy-load — MissingGreenlet in async context (same
            # class of bug as app/workers/vscu_poller.py's fix, see project history).
            owner_name = owner.name
            await db.rollback()
            return _failure(
                branch_in,
                f"(tin, bhfId) is already registered under a different customer "
                f"({owner_name!r}) — check for a data-entry mistake",
            )

        if branch_in.device_type == "VSCU":
            if branch_in.base_url is None:
                await db.rollback()
                return _failure(branch_in, "base_url is required for a VSCU branch")
            vscu_client = VSCUClient(db=db)
            try:
                await VSCUInitializationService(db, vscu_client).initialize(
                    branch.id, base_url=branch_in.base_url
                )
            finally:
                await vscu_client.aclose()

        await db.commit()
        return BranchRegistrationOutcome(
            branch_code=branch_in.branch_code,
            device_type=branch_in.device_type,
            status="REGISTERED",
            branch_id=branch.id,
            error=None,
        )
    except cmc_key_manager.DeviceAlreadyProvisionedButUnknown as exc:
        await db.rollback()
        return _failure(branch_in, str(exc))
    except KraError as exc:
        await db.rollback()
        return _failure(branch_in, f"KRA error {exc.result_cd}: {exc.result_msg}")
    except VSCUError as exc:
        await db.rollback()
        # str(exc), not exc.context.result_cd/result_msg: VSCUConfigurationError and
        # VSCUTransportError never carry those (no request was ever attempted, or it failed
        # before any VSCU envelope existed) — reaching into context directly produces the
        # useless "VSCU error None: None" for exactly those two subtypes. str(exc) is always
        # complete: either the custom message those subtypes set, or VSCUError's own
        # resultCd/resultMsg-formatted default for a real KRA/VSCU response.
        return _failure(branch_in, f"VSCU error: {exc}")
    except ValueError as exc:
        await db.rollback()
        return _failure(branch_in, str(exc))


async def onboard_customer(
    db: AsyncSession, name: str, branches: list[BranchRegistrationInput]
) -> tuple[Customer, list[BranchRegistrationOutcome]]:
    kra = KraAdapter(db=db)
    outcomes: list[BranchRegistrationOutcome] = []
    try:
        for branch_in in branches:
            outcomes.append(await register_branch(db, kra, branch_in))
    finally:
        await kra.aclose()

    successful_branch_ids = [o.branch_id for o in outcomes if o.branch_id is not None]
    customer = await customer_service.create_customer(db, name, successful_branch_ids)
    await db.commit()

    logger.info(
        "Customer %s (%s) onboarded: %d/%d branches registered",
        customer.id,
        name,
        len(successful_branch_ids),
        len(branches),
    )
    return customer, outcomes
