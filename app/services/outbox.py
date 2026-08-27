"""Outbox pattern helpers (spec §14): write to `outbox` in the same DB transaction as the
business record, return 202 Accepted, dispatch via outbox_dispatcher with backoff. Endpoints
here always call enqueue() and the caller's own business-record insert in the same request —
neither is committed until the endpoint commits both together.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Outbox


async def enqueue(
    db: AsyncSession, endpoint: str, branch_id: uuid.UUID, payload: dict[str, object]
) -> Outbox:
    row = Outbox(endpoint=endpoint, branch_id=branch_id, payload=payload)
    db.add(row)
    await db.flush()
    return row
