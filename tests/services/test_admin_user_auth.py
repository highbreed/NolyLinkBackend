"""Tests for admin_user_auth: password hashing, login, JWT issuance/verification, and the
type-claim isolation that keeps a client OAuth2 token from ever working as an admin token
even though both share JWT_SECRET_KEY (app/services/admin_user_auth.py).
"""

from datetime import UTC, datetime, timedelta

import pytest
from jose import jwt

from app.core.config import Settings
from app.services import admin_user_auth
from app.services.client_auth import hash_client_secret  # unrelated helper, just for contrast

SETTINGS = Settings(jwt_secret_key="test-secret", admin_jwt_access_token_expire_minutes=60)


async def test_create_and_authenticate_round_trip(db_session):
    user = await admin_user_auth.create_admin_user(
        db_session, "amina", "correct horse battery", "Amina K."
    )
    assert user.password_hash != "correct horse battery"  # never stored in plaintext

    authenticated = await admin_user_auth.authenticate_admin_user(
        db_session, "amina", "correct horse battery"
    )
    assert authenticated.id == user.id
    assert authenticated.last_login_at is not None


async def test_authenticate_wrong_password_raises(db_session):
    await admin_user_auth.create_admin_user(db_session, "amina", "correct horse battery", None)
    with pytest.raises(admin_user_auth.InvalidCredentialsError):
        await admin_user_auth.authenticate_admin_user(db_session, "amina", "wrong password")


async def test_authenticate_unknown_username_raises(db_session):
    with pytest.raises(admin_user_auth.InvalidCredentialsError):
        await admin_user_auth.authenticate_admin_user(db_session, "nobody", "irrelevant")


async def test_duplicate_username_raises(db_session):
    await admin_user_auth.create_admin_user(db_session, "amina", "password-one", None)
    with pytest.raises(admin_user_auth.UsernameAlreadyExistsError):
        await admin_user_auth.create_admin_user(db_session, "amina", "password-two", None)


async def test_deactivated_user_cannot_authenticate(db_session):
    user = await admin_user_auth.create_admin_user(
        db_session, "amina", "correct horse battery", None
    )
    await admin_user_auth.deactivate_admin_user(db_session, user.id)

    with pytest.raises(admin_user_auth.InvalidCredentialsError):
        await admin_user_auth.authenticate_admin_user(db_session, "amina", "correct horse battery")


async def test_reactivate_restores_login(db_session):
    user = await admin_user_auth.create_admin_user(
        db_session, "amina", "correct horse battery", None
    )
    await admin_user_auth.deactivate_admin_user(db_session, user.id)
    await admin_user_auth.reactivate_admin_user(db_session, user.id)

    authenticated = await admin_user_auth.authenticate_admin_user(
        db_session, "amina", "correct horse battery"
    )
    assert authenticated.id == user.id


async def test_issued_token_resolves_back_to_the_same_user(db_session):
    user = await admin_user_auth.create_admin_user(
        db_session, "amina", "correct horse battery", None
    )
    token, expires_in = admin_user_auth.issue_admin_token(user, SETTINGS)
    assert expires_in == 3600

    resolved = await admin_user_auth.get_admin_user_from_token(db_session, token, SETTINGS)
    assert resolved is not None
    assert resolved.id == user.id


async def test_deactivating_a_user_invalidates_their_existing_token(db_session):
    """A token issued before deactivation must stop working immediately, not just once it
    naturally expires — get_admin_user_from_token re-checks is_active on every call.
    """
    user = await admin_user_auth.create_admin_user(
        db_session, "amina", "correct horse battery", None
    )
    token, _ = admin_user_auth.issue_admin_token(user, SETTINGS)
    await admin_user_auth.deactivate_admin_user(db_session, user.id)

    resolved = await admin_user_auth.get_admin_user_from_token(db_session, token, SETTINGS)
    assert resolved is None


async def test_expired_token_is_rejected(db_session):
    user = await admin_user_auth.create_admin_user(
        db_session, "amina", "correct horse battery", None
    )
    now = datetime.now(UTC)
    expired_payload = {
        "sub": str(user.id),
        "type": admin_user_auth.ADMIN_TOKEN_TYPE,
        "iat": now - timedelta(hours=2),
        "exp": now - timedelta(hours=1),
    }
    expired_token = jwt.encode(
        expired_payload, SETTINGS.jwt_secret_key, algorithm=SETTINGS.jwt_algorithm
    )

    resolved = await admin_user_auth.get_admin_user_from_token(db_session, expired_token, SETTINGS)
    assert resolved is None


async def test_client_style_token_is_never_valid_as_an_admin_token(db_session):
    """A client OAuth2 token (client_auth.py) has no "type" claim at all — this proves it can
    never be replayed against an admin endpoint even though both share JWT_SECRET_KEY.
    """
    user = await admin_user_auth.create_admin_user(
        db_session, "amina", "correct horse battery", None
    )
    now = datetime.now(UTC)
    client_style_payload = {"sub": str(user.id), "iat": now, "exp": now + timedelta(minutes=30)}
    client_style_token = jwt.encode(
        client_style_payload, SETTINGS.jwt_secret_key, algorithm=SETTINGS.jwt_algorithm
    )

    resolved = await admin_user_auth.get_admin_user_from_token(
        db_session, client_style_token, SETTINGS
    )
    assert resolved is None


async def test_garbage_token_is_rejected(db_session):
    resolved = await admin_user_auth.get_admin_user_from_token(db_session, "not-a-jwt", SETTINGS)
    assert resolved is None


async def test_list_admin_users_returns_everyone_alphabetically(db_session):
    await admin_user_auth.create_admin_user(db_session, "zoe", "password-one", None)
    await admin_user_auth.create_admin_user(db_session, "amina", "password-two", None)

    users = await admin_user_auth.list_admin_users(db_session)
    usernames = [u.username for u in users]
    assert usernames.index("amina") < usernames.index("zoe")


def test_hash_client_secret_and_hash_password_are_different_schemes():
    """Sanity check on the design rationale in both modules' docstrings: client secrets use
    a fast deterministic sha256 hash (O(1) lookup), admin passwords use salted bcrypt (two
    calls with the same input never produce the same hash).
    """
    assert hash_client_secret("shared-input") == hash_client_secret("shared-input")
    assert admin_user_auth.hash_password("shared-input") != admin_user_auth.hash_password(
        "shared-input"
    )
