"""Admin portal login: password hashing/verification and JWT issuance for AdminUser accounts.

Sits alongside the static X-Admin-Api-Key (app/core/admin_auth.py), not in place of it —
either credential grants the same admin access, so existing scripts/CI using the key keep
working. Passwords are hashed with bcrypt directly, not sha256 like
Client.client_secret_hash — a human password is low-entropy and needs slow, salted hashing to
resist brute force, unlike a generated high-entropy client secret (see client_auth.py's
docstring, which flagged this exact distinction back in Phase 9).

Uses the `bcrypt` package directly rather than passlib's CryptContext wrapper: passlib 1.7.4
(last released 2020, effectively unmaintained) probes `bcrypt.__about__.__version__` at
import time to detect the backend version, which no longer exists on bcrypt>=4.1 — that probe
failure cascades into passlib's own internal self-test raising instead of just logging a
warning, breaking every hash/verify call. bcrypt's own hashpw/checkpw API is stable and
doesn't need that shim.

Admin JWTs share a signing key with client JWTs (client_auth.py) but carry a "type": "admin"
claim client tokens never have, so a compromised/malicious client token can never be replayed
against an admin endpoint even though both are signed with the same JWT_SECRET_KEY.
"""

import uuid
from datetime import UTC, datetime, timedelta

import bcrypt
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.models import AdminUser

ADMIN_TOKEN_TYPE = "admin"


class UsernameAlreadyExistsError(Exception):
    pass


class InvalidCredentialsError(Exception):
    pass


class AdminUserNotFoundError(Exception):
    pass


def hash_password(raw_password: str) -> str:
    encoded = raw_password.encode("utf-8")
    if len(encoded) > 72:
        # bcrypt's own hard limit — raises rather than silently truncating (silent truncation
        # would make two passwords sharing the same 72-byte prefix equivalent).
        raise ValueError("Password must be at most 72 bytes")
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("ascii")


def verify_password(raw_password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(raw_password.encode("utf-8"), password_hash.encode("ascii"))


async def create_admin_user(
    db: AsyncSession, username: str, raw_password: str, full_name: str | None
) -> AdminUser:
    existing = await db.scalar(select(AdminUser).where(AdminUser.username == username))
    if existing is not None:
        raise UsernameAlreadyExistsError(f"Username {username!r} is already taken")

    user = AdminUser(
        username=username, password_hash=hash_password(raw_password), full_name=full_name
    )
    db.add(user)
    await db.flush()
    return user


async def authenticate_admin_user(db: AsyncSession, username: str, raw_password: str) -> AdminUser:
    user = await db.scalar(select(AdminUser).where(AdminUser.username == username))
    if user is None or not user.is_active or not verify_password(raw_password, user.password_hash):
        # Deliberately the same error for "no such user" and "wrong password" — distinguishing
        # them lets an attacker enumerate valid usernames.
        raise InvalidCredentialsError("Invalid username or password")

    user.last_login_at = datetime.now(UTC)
    await db.flush()
    return user


def issue_admin_token(user: AdminUser, settings: Settings | None = None) -> tuple[str, int]:
    settings = settings or get_settings()
    now = datetime.now(UTC)
    expires_delta = timedelta(minutes=settings.admin_jwt_access_token_expire_minutes)
    payload = {
        "sub": str(user.id),
        "type": ADMIN_TOKEN_TYPE,
        "iat": now,
        "exp": now + expires_delta,
    }
    token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return token, int(expires_delta.total_seconds())


async def get_admin_user_from_token(
    db: AsyncSession, token: str, settings: Settings | None = None
) -> AdminUser | None:
    """Returns None on any failure (bad signature, expired, wrong token type, unknown or
    deactivated user) rather than raising — callers (app/core/admin_auth.py) treat "not a
    valid admin token" as one uniform case, same as a missing/wrong X-Admin-Api-Key.
    """
    settings = settings or get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
        if payload.get("type") != ADMIN_TOKEN_TYPE:
            return None
        user_id = uuid.UUID(payload["sub"])
    except (JWTError, KeyError, ValueError):
        return None

    user = await db.get(AdminUser, user_id)
    if user is None or not user.is_active:
        return None
    return user


async def list_admin_users(db: AsyncSession) -> list[AdminUser]:
    result = await db.execute(select(AdminUser).order_by(AdminUser.username))
    return list(result.scalars().all())


async def update_admin_user(
    db: AsyncSession,
    user_id: uuid.UUID,
    username: str | None,
    raw_password: str | None,
    full_name: str | None,
) -> AdminUser:
    """Only fields that are not None are changed. Username uniqueness is re-checked against
    every other row (not just skipped) — matches create_admin_user's own check.
    """
    user = await db.get(AdminUser, user_id)
    if user is None:
        raise AdminUserNotFoundError(f"No admin user with id={user_id}")

    if username is not None and username != user.username:
        existing = await db.scalar(select(AdminUser).where(AdminUser.username == username))
        if existing is not None:
            raise UsernameAlreadyExistsError(f"Username {username!r} is already taken")
        user.username = username

    if raw_password is not None:
        user.password_hash = hash_password(raw_password)

    if full_name is not None:
        user.full_name = full_name

    await db.flush()
    return user


async def deactivate_admin_user(db: AsyncSession, user_id: uuid.UUID) -> AdminUser:
    user = await db.get(AdminUser, user_id)
    if user is None:
        raise AdminUserNotFoundError(f"No admin user with id={user_id}")
    user.is_active = False
    await db.flush()
    return user


async def reactivate_admin_user(db: AsyncSession, user_id: uuid.UUID) -> AdminUser:
    user = await db.get(AdminUser, user_id)
    if user is None:
        raise AdminUserNotFoundError(f"No admin user with id={user_id}")
    user.is_active = True
    await db.flush()
    return user
