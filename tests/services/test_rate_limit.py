"""Tests for the per-client Redis rate limiter (spec §15). Runs against a real Redis
instance (see get_settings().redis_url) — each test uses a fresh, randomly-generated client
id, so counters can never collide with another test or another process using the same Redis.
"""

import uuid

import pytest
from fastapi import HTTPException

from app.core.config import get_settings
from app.services.rate_limit import enforce_rate_limit

# Every key this module creates is ratelimit:{a fresh random uuid4 per test}:{window} and
# carries a 60s TTL (see enforce_rate_limit) — no cleanup needed, and no risk to any other
# key on the shared Redis instance these tests run against.


@pytest.fixture(autouse=True)
async def _low_rate_limit(monkeypatch):
    """3/minute makes the limit reachable in a handful of calls instead of 120."""
    settings = get_settings()
    monkeypatch.setattr(settings, "client_rate_limit_per_minute", 3)


class _FakeClient:
    def __init__(self) -> None:
        self.id = uuid.uuid4()


async def test_requests_under_the_limit_succeed():
    client = _FakeClient()
    for _ in range(3):
        result = await enforce_rate_limit(client=client)  # type: ignore[arg-type]
        assert result is client


async def test_request_over_the_limit_raises_429():
    client = _FakeClient()
    for _ in range(3):
        await enforce_rate_limit(client=client)  # type: ignore[arg-type]

    with pytest.raises(HTTPException) as exc_info:
        await enforce_rate_limit(client=client)  # type: ignore[arg-type]
    assert exc_info.value.status_code == 429


async def test_different_clients_have_independent_limits():
    client_a = _FakeClient()
    client_b = _FakeClient()

    for _ in range(3):
        await enforce_rate_limit(client=client_a)  # type: ignore[arg-type]

    # client_b's own limit hasn't been touched, so this must still succeed.
    result = await enforce_rate_limit(client=client_b)  # type: ignore[arg-type]
    assert result is client_b
