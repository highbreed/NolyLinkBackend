"""Shared admin gate for ops-only endpoints (device init, billing, admin user management) —
none of these are ever reachable by SAP/ERP clients, so they sit outside Phase 9's client
OAuth2 system entirely (spec §12.2's device-init reasoning applies equally to billing and
portal admin management: none of it must be reachable, or manipulable, by the tenants it bills).

Accepts either the static X-Admin-Api-Key or a portal admin's bearer JWT
(app/services/admin_user_auth.py) — the login system sits alongside the static key, not in
place of it, so existing scripts/CI keep working unchanged.
"""

from fastapi import Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_db
from app.services import admin_user_auth


async def require_admin(
    x_admin_api_key: str = Header(default=""),
    authorization: str = Header(default=""),
    db: AsyncSession = Depends(get_db),
) -> str:
    """Returns a human-readable label for who authenticated — "api-key" or
    "user:<username>" — so mutating admin endpoints can log who acted, without a second round
    of credential-checking (FastAPI caches a dependency's result per request, so declaring
    this again as an explicit parameter on top of the router-level `dependencies=[...]` entry
    doesn't re-run this body).
    """
    configured = get_settings().admin_api_key
    if configured and x_admin_api_key == configured:
        return "api-key"

    if authorization.startswith("Bearer "):
        admin_user = await admin_user_auth.get_admin_user_from_token(
            db, authorization.removeprefix("Bearer ")
        )
        if admin_user is not None:
            return f"user:{admin_user.username}"

    raise HTTPException(status_code=403, detail="Admin access required")
