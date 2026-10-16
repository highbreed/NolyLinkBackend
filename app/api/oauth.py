"""POST /oauth/token — OAuth2 client-credentials grant (spec §15).

Unversioned (no /v1 prefix), matching the spec's own literal path and standard OAuth2
convention (RFC 6749 §4.4) of a single token endpoint outside API version namespaces.
"""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, status
from jose import jwt
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Client
from app.db.session import get_db
from app.services.client_auth import hash_client_secret

router = APIRouter()


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


@router.post("/token", response_model=TokenResponse)
async def issue_token(
    grant_type: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(...),
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    if grant_type != "client_credentials":
        raise HTTPException(status_code=400, detail="unsupported_grant_type")

    try:
        client_uuid = uuid.UUID(client_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_client"
        ) from None

    client = await db.get(Client, client_uuid)
    if (
        client is None
        or client.client_secret_hash != hash_client_secret(client_secret)
        or client.revoked_at is not None
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_client")

    settings = get_settings()
    now = datetime.now(UTC)
    expires_delta = timedelta(minutes=settings.jwt_access_token_expire_minutes)
    payload = {"sub": str(client.id), "iat": now, "exp": now + expires_delta}
    token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)

    return TokenResponse(access_token=token, expires_in=int(expires_delta.total_seconds()))
