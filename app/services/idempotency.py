"""Dedupe mutating client requests by (client_id, external_ref, endpoint) (spec §13).

Uses a claim-then-store pattern rather than a plain read-then-write check: two concurrent
requests with the same external_ref must not both reach KRA (a double-submitted sales
invoice is a real fiscal problem, not just a wasted call). claim_or_get_cached() atomically
inserts a placeholder row (kra_response still NULL); whichever request wins the race proceeds
to call KRA and then store_response(), the loser sees either the finished cached response or,
in the rare case it lands mid-flight, an IdempotencyInProgress it can turn into a 409/retry.
"""

import uuid

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import IdempotencyKey


class IdempotencyInProgress(Exception):
    """Another request with the same (client_id, external_ref, endpoint) is still being
    processed — the caller should tell the client to retry shortly rather than proceed.
    """


async def claim_or_get_cached(
    db: AsyncSession, client_id: uuid.UUID, external_ref: str, endpoint: str
) -> dict[str, object] | None:
    """Returns the cached response if this key was already completed. Returns None if this
    call claimed the slot — the caller should proceed and then call store_response().
    """
    stmt = (
        pg_insert(IdempotencyKey)
        .values(client_id=client_id, external_ref=external_ref, endpoint=endpoint)
        .on_conflict_do_nothing(
            index_elements=[
                IdempotencyKey.client_id,
                IdempotencyKey.external_ref,
                IdempotencyKey.endpoint,
            ]
        )
    )
    result = await db.execute(stmt)
    if result.rowcount == 1:  # type: ignore[attr-defined]
        return None

    existing = await db.scalar(
        select(IdempotencyKey).where(
            IdempotencyKey.client_id == client_id,
            IdempotencyKey.external_ref == external_ref,
            IdempotencyKey.endpoint == endpoint,
        )
    )
    if existing is not None and existing.kra_response is not None:
        return existing.kra_response
    raise IdempotencyInProgress(
        f"external_ref={external_ref!r} for endpoint={endpoint!r} is already being processed"
    )


async def store_response(
    db: AsyncSession,
    client_id: uuid.UUID,
    external_ref: str,
    endpoint: str,
    response: dict[str, object],
) -> None:
    await db.execute(
        update(IdempotencyKey)
        .where(
            IdempotencyKey.client_id == client_id,
            IdempotencyKey.external_ref == external_ref,
            IdempotencyKey.endpoint == endpoint,
        )
        .values(kra_response=response)
    )


async def release_claim(
    db: AsyncSession, client_id: uuid.UUID, external_ref: str, endpoint: str
) -> None:
    """Deletes an unfinished claim (kra_response still NULL) so a retry isn't permanently
    blocked by IdempotencyInProgress. Callers use this when the winning request fails before
    reaching store_response — e.g. a KraValidationError that should let the client correct
    and resubmit under the same external_ref, rather than raising KraTransientServerError.
    """
    await db.execute(
        delete(IdempotencyKey).where(
            IdempotencyKey.client_id == client_id,
            IdempotencyKey.external_ref == external_ref,
            IdempotencyKey.endpoint == endpoint,
            IdempotencyKey.kra_response.is_(None),
        )
    )
