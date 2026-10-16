"""POST /admin/login — backend portal login for human ops staff — and admin user
management. Unversioned, matching /oauth/token's convention for infra-level auth that sits
outside the /v1 client API surface.

Sits alongside the static X-Admin-Api-Key (app/core/admin_auth.py), not in place of it: the
first admin user has to be created using the static key (there's no public signup), and every
admin endpoint here (except login itself) accepts either credential.
"""

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.admin_auth import require_admin
from app.db.models import AdminUser
from app.db.session import get_db
from app.services import admin_user_auth

logger = logging.getLogger(__name__)

router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)) -> LoginResponse:
    try:
        user = await admin_user_auth.authenticate_admin_user(db, payload.username, payload.password)
        await db.commit()
    except admin_user_auth.InvalidCredentialsError as exc:
        await db.rollback()
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    token, expires_in = admin_user_auth.issue_admin_token(user)
    return LoginResponse(access_token=token, expires_in=expires_in)


class AdminUserCreateRequest(BaseModel):
    username: str
    password: str = Field(min_length=8, max_length=72)
    full_name: str | None = None


class AdminUserOut(BaseModel):
    id: uuid.UUID
    username: str
    full_name: str | None
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None


def _to_out(user: AdminUser) -> AdminUserOut:
    return AdminUserOut(
        id=user.id,
        username=user.username,
        full_name=user.full_name,
        is_active=user.is_active,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


@router.post("/users", response_model=AdminUserOut, status_code=201)
async def create_user(
    payload: AdminUserCreateRequest,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminUserOut:
    try:
        user = await admin_user_auth.create_admin_user(
            db, payload.username, payload.password, payload.full_name
        )
        await db.commit()
    except admin_user_auth.UsernameAlreadyExistsError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    logger.info("Admin user %r created by %s", payload.username, admin_actor)
    return _to_out(user)


@router.get("/users", response_model=list[AdminUserOut], dependencies=[Depends(require_admin)])
async def list_users(db: AsyncSession = Depends(get_db)) -> list[AdminUserOut]:
    users = await admin_user_auth.list_admin_users(db)
    return [_to_out(u) for u in users]


class AdminUserUpdateRequest(BaseModel):
    username: str | None = None
    password: str | None = Field(default=None, min_length=8, max_length=72)
    full_name: str | None = None


@router.patch("/users/{user_id}", response_model=AdminUserOut)
async def update_user(
    user_id: uuid.UUID,
    payload: AdminUserUpdateRequest,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminUserOut:
    try:
        user = await admin_user_auth.update_admin_user(
            db, user_id, payload.username, payload.password, payload.full_name
        )
        await db.commit()
    except admin_user_auth.AdminUserNotFoundError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except admin_user_auth.UsernameAlreadyExistsError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    logger.info("Admin user %s updated by %s", user_id, admin_actor)
    return _to_out(user)


@router.post("/users/{user_id}/deactivate", response_model=AdminUserOut)
async def deactivate_user(
    user_id: uuid.UUID,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminUserOut:
    try:
        user = await admin_user_auth.deactivate_admin_user(db, user_id)
        await db.commit()
    except admin_user_auth.AdminUserNotFoundError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    logger.info("Admin user %r deactivated by %s", user.username, admin_actor)
    return _to_out(user)


@router.post("/users/{user_id}/reactivate", response_model=AdminUserOut)
async def reactivate_user(
    user_id: uuid.UUID,
    admin_actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AdminUserOut:
    try:
        user = await admin_user_auth.reactivate_admin_user(db, user_id)
        await db.commit()
    except admin_user_auth.AdminUserNotFoundError as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    logger.info("Admin user %r reactivated by %s", user.username, admin_actor)
    return _to_out(user)
