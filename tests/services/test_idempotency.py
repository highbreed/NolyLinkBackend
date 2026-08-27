"""Tests for the idempotency claim-then-store pattern (spec §13)."""

import pytest

from app.db.models import Client
from app.services import idempotency


@pytest.fixture
async def client_id(db_session):
    client = Client(name="TEST-CLIENT", client_secret_hash="irrelevant", allowed_branch_ids=[])
    db_session.add(client)
    await db_session.flush()
    return client.id


async def test_first_call_claims_and_returns_none(db_session, client_id):
    result = await idempotency.claim_or_get_cached(db_session, client_id, "REF-1", "/x")
    assert result is None


async def test_concurrent_call_before_store_raises_in_progress(db_session, client_id):
    await idempotency.claim_or_get_cached(db_session, client_id, "REF-2", "/x")

    with pytest.raises(idempotency.IdempotencyInProgress):
        await idempotency.claim_or_get_cached(db_session, client_id, "REF-2", "/x")


async def test_call_after_store_returns_cached_response(db_session, client_id):
    await idempotency.claim_or_get_cached(db_session, client_id, "REF-3", "/x")
    await idempotency.store_response(db_session, client_id, "REF-3", "/x", {"rcptSign": "abc"})

    result = await idempotency.claim_or_get_cached(db_session, client_id, "REF-3", "/x")
    assert result == {"rcptSign": "abc"}


async def test_release_claim_allows_a_fresh_attempt(db_session, client_id):
    await idempotency.claim_or_get_cached(db_session, client_id, "REF-4", "/x")
    await idempotency.release_claim(db_session, client_id, "REF-4", "/x")

    result = await idempotency.claim_or_get_cached(db_session, client_id, "REF-4", "/x")
    assert result is None


async def test_release_claim_does_not_touch_a_completed_entry(db_session, client_id):
    await idempotency.claim_or_get_cached(db_session, client_id, "REF-5", "/x")
    await idempotency.store_response(db_session, client_id, "REF-5", "/x", {"rcptSign": "xyz"})

    await idempotency.release_claim(db_session, client_id, "REF-5", "/x")

    result = await idempotency.claim_or_get_cached(db_session, client_id, "REF-5", "/x")
    assert result == {"rcptSign": "xyz"}


async def test_different_endpoints_are_independent_keys(db_session, client_id):
    first = await idempotency.claim_or_get_cached(db_session, client_id, "REF-6", "/x")
    second = await idempotency.claim_or_get_cached(db_session, client_id, "REF-6", "/y")
    assert first is None
    assert second is None
