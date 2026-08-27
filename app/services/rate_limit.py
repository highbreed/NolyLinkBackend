"""Per-client rate limiting (spec §15) via a Redis fixed-window counter.

The build prompt names "Redis token bucket / slowapi" as the two options. slowapi's key_func
is oriented around request-level data available before dependency injection (IP address,
headers) — plugging in our JWT-derived client identity would mean decoding the token twice
(once for slowapi, once for get_current_client). A small dependency that runs after
get_current_client and shares its result avoids that duplication. It's a fixed window, not a
true leaky/token bucket (no smoothing across the window boundary) — simpler to reason about
and test, and enough to satisfy "per-client rate limiting" without pulling in slowapi's own
request-matching machinery for a single global limit.
"""

import time
from functools import lru_cache

import redis.asyncio as redis
from fastapi import Depends, HTTPException, status

from app.core.config import get_settings
from app.db.models import Client
from app.services.client_auth import get_current_client


@lru_cache
def get_redis() -> redis.Redis:
    return redis.from_url(  # type: ignore[no-any-return,no-untyped-call]
        get_settings().redis_url, decode_responses=True
    )


async def enforce_rate_limit(client: Client = Depends(get_current_client)) -> Client:
    """Returns the same Client get_current_client would, so this drops in as a direct
    replacement for it on every client-facing endpoint.
    """
    settings = get_settings()
    r = get_redis()
    window = int(time.time() // 60)
    key = f"ratelimit:{client.id}:{window}"

    count = await r.incr(key)
    if count == 1:
        await r.expire(key, 60)

    if count > settings.client_rate_limit_per_minute:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded — try again shortly",
        )
    return client
