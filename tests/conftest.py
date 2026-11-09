"""Shared pytest fixtures: an async DB session against a disposable schema.

Tests run against Settings.resolved_test_database_url (DATABASE_URL's database name with a
"_test" suffix, unless TEST_DATABASE_URL is set explicitly) — never against DATABASE_URL
itself, so a test run's create_all/drop_all can never touch real dev/prod data. The test
database is created automatically if it doesn't exist. Tables are created once per test
session and each test runs inside a transaction that's rolled back, so tests don't need to
clean up after themselves and can run in any order.
"""

from collections.abc import AsyncGenerator

import asyncpg
import pytest_asyncio
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings
from app.db import session as db_session_module
from app.db.models import Base


async def _ensure_database_exists(test_url: str) -> None:
    url = make_url(test_url)
    assert url.database is not None
    conn = await asyncpg.connect(
        user=url.username,
        password=url.password,
        host=url.host,
        port=url.port,
        database="postgres",
    )
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", url.database)
    finally:
        await conn.close()
    if not exists:
        raise RuntimeError(
            f"Test database {url.database!r} does not exist. The app DB role usually lacks "
            f"CREATEDB, so create it once as a superuser:\n"
            f'  CREATE DATABASE "{url.database}" OWNER {url.username};'
        )


@pytest_asyncio.fixture(scope="session")
async def db_engine() -> AsyncGenerator[AsyncEngine, None]:
    settings = get_settings()
    test_url = settings.resolved_test_database_url
    assert test_url != settings.database_url, (
        "resolved_test_database_url must never equal database_url — "
        "this fixture runs create_all/drop_all and would destroy real data"
    )

    await _ensure_database_exists(test_url)

    engine = create_async_engine(test_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine, monkeypatch) -> AsyncGenerator[AsyncSession, None]:
    async with db_engine.connect() as conn:
        trans = await conn.begin()
        session_factory = async_sessionmaker(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        # AuditLogMiddleware (app/core/audit.py) opens its own session via
        # app.db.session.AsyncSessionLocal on every request, independent of any per-endpoint
        # get_db override — unlike outbox_dispatcher's _dispatch_pending, nothing calls it
        # directly with an explicit session_factory, since it's wired into every HTTP
        # round-trip, not invoked directly in tests. Binding it to this SAME connection (not
        # a fresh one from db_engine) is required, not just nice-to-have: a fresh connection
        # can't see this transaction's uncommitted fixture rows (e.g. a just-created Client),
        # so a real request touching client_call_log's client_id FK would fail otherwise.
        monkeypatch.setattr(db_session_module, "AsyncSessionLocal", session_factory)
        async with session_factory() as session:
            yield session
        await trans.rollback()
