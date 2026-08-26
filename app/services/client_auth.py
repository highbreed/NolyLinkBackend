"""Minimal, provisional client identification via a shared-secret API key.

Spec §13's idempotency_keys table is keyed by (client_id, external_ref, endpoint) — dedup
without knowing "which client" isn't meaningful. Phase 9 owns the real inbound auth system
(OAuth2 client-credentials, rate limiting, branch allow-listing per spec §15); this is just
enough of a client-lookup primitive to make idempotency correct ahead of that, the same way
Phase 4's admin gate stood in for Phase 9's auth on the device-init endpoint.

API keys are hashed with sha256, not bcrypt/passlib (already a project dependency) — bcrypt is
for low-entropy user passwords where slow, salted hashing resists brute force; a generated API
key is already high-entropy, and sha256 allows an O(1) lookup by hash instead of checking every
row. There's no client-provisioning endpoint yet, so clients.api_key_hash is populated by
inserting a row directly for now.
"""

import hashlib

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Client
from app.db.session import get_db


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


async def get_current_client(
    x_api_key: str = Header(...),
    db: AsyncSession = Depends(get_db),
) -> Client:
    client = await db.scalar(select(Client).where(Client.api_key_hash == hash_api_key(x_api_key)))
    if client is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return client
