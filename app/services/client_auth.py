"""Client authentication (spec §15): OAuth2 client-credentials JWT verification, plus
per-client branch allow-listing.

client_secret is hashed with sha256, not bcrypt/passlib (already a project dependency) —
bcrypt is for low-entropy user passwords where slow, salted hashing resists brute force; an
OAuth2 client_secret is already a generated high-entropy value, and sha256 allows an O(1)
lookup by hash instead of checking every row (same reasoning as Phase 7's original API-key
lookup, which this replaces).
"""

import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.db.models import Branch, Client
from app.db.session import get_db
from app.services import billing_service, cmc_key_manager
from app.services.secret_hashing import hash_client_secret as hash_client_secret

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/oauth/token")


async def get_current_client(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> Client:
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
        client_id = uuid.UUID(payload["sub"])
    except (JWTError, KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token"
        ) from exc

    # Not db.get(): its `options` are only applied when a query is actually emitted, which
    # db.get() skips whenever the Client row is already in this session's identity map (e.g.
    # a test fixture flushed it on the same session an HTTP call then reuses) — client.customer
    # would silently stay unloaded and the plain attribute access below would trigger an
    # implicit lazy load, which raises MissingGreenlet in async context. An explicit select()
    # always goes through execute(), so its selectinload is never skipped.
    client = await db.scalar(
        select(Client).where(Client.id == client_id).options(selectinload(Client.customer))
    )
    if client is None or client.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token"
        )

    try:
        await billing_service.enforce_active_subscription(db, client.customer, settings)
    except billing_service.SubscriptionBlockedError as exc:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=str(exc)) from exc

    return client


async def get_authorized_branch(db: AsyncSession, client: Client, branch_code: str) -> Branch:
    """Resolves branch_code and enforces spec §15's per-client branch allow-listing — a
    client with a valid token can still only act on branches its Customer's allowed_branch_ids
    lists (2026-09-01: branch access moved from Client to Customer — see Customer's docstring
    in app/db/models.py — since it's a property of the paying business, not of any one of its
    connectors).
    """
    try:
        branch = await cmc_key_manager.get_branch_credentials(db, branch_code)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if branch.id not in client.customer.allowed_branch_ids:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Client {client.name!r} is not allowed to access branch {branch_code!r}",
        )
    return branch
