"""Per-Customer restriction on which KRA reference codes a connector may submit
(CustomerCodeAllowlist, 2026-09-18) — see that model's docstring in app/db/models.py for the
"no rows == unrestricted" semantics.

Deliberately has no dependency on app/services/mapping/generic_mapper.py (which is this
module's only caller) to avoid a circular import — generic_mapper wraps CodeNotAllowedError as
its own CodeLookupError, the exception every endpoint already catches as a 422.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CustomerCodeAllowlist


class CodeNotAllowedError(Exception):
    pass


async def enforce_customer_code_allowlist(
    db: AsyncSession, customer_id: uuid.UUID, classification: str, code: str
) -> None:
    """No-op if the customer has no configured allow-list rows for this classification."""
    result = await db.execute(
        select(CustomerCodeAllowlist.code).where(
            CustomerCodeAllowlist.customer_id == customer_id,
            CustomerCodeAllowlist.classification == classification,
        )
    )
    allowed = set(result.scalars().all())
    if allowed and code not in allowed:
        raise CodeNotAllowedError(
            f"Customer is not permitted to use {classification!r} code {code!r}"
        )


async def get_allowed_codes(
    db: AsyncSession, customer_id: uuid.UUID, classification: str
) -> set[str] | None:
    """`None` means unrestricted (no rows configured for this classification) — the
    single-classification counterpart to get_code_allowlist's full-customer query, used by
    the client-facing browsing endpoints (app/api/v1/endpoints/codes.py, item_classes.py) to
    filter what a restricted customer sees to what it can actually submit.
    """
    result = await db.execute(
        select(CustomerCodeAllowlist.code).where(
            CustomerCodeAllowlist.customer_id == customer_id,
            CustomerCodeAllowlist.classification == classification,
        )
    )
    codes = set(result.scalars().all())
    return codes or None


async def get_code_allowlist(db: AsyncSession, customer_id: uuid.UUID) -> dict[str, list[str]]:
    """One entry per classification with any configured rows — omits classifications with none
    (unrestricted), matching the "no rows == unrestricted" model.
    """
    result = await db.execute(
        select(CustomerCodeAllowlist)
        .where(CustomerCodeAllowlist.customer_id == customer_id)
        .order_by(CustomerCodeAllowlist.classification, CustomerCodeAllowlist.code)
    )
    grouped: dict[str, list[str]] = {}
    for row in result.scalars().all():
        grouped.setdefault(row.classification, []).append(row.code)
    return grouped


async def set_code_allowlist(
    db: AsyncSession, customer_id: uuid.UUID, classification: str, codes: list[str]
) -> None:
    """Replace-whole-set for (customer_id, classification) — delete-then-insert, matching
    customer_service.update_customer's replace semantics for allowed_branch_ids. An empty
    `codes` list clears the restriction entirely (back to unrestricted).
    """
    await db.execute(
        CustomerCodeAllowlist.__table__.delete().where(
            CustomerCodeAllowlist.customer_id == customer_id,
            CustomerCodeAllowlist.classification == classification,
        )
    )
    for code in dict.fromkeys(codes):  # de-dupe, preserve order
        db.add(
            CustomerCodeAllowlist(customer_id=customer_id, classification=classification, code=code)
        )
    await db.flush()
