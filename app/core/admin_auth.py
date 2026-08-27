"""Shared admin gate for ops-only endpoints (device init, billing) — none of these are ever
reachable by SAP/ERP clients, so they sit outside Phase 9's client OAuth2 system entirely
(spec §12.2's device-init reasoning applies equally to billing: it must not be reachable, or
manipulable, by the tenants it bills).
"""

from fastapi import Header, HTTPException

from app.core.config import get_settings


async def require_admin(x_admin_api_key: str = Header(default="")) -> None:
    configured = get_settings().admin_api_key
    if not configured or x_admin_api_key != configured:
        raise HTTPException(status_code=403, detail="Admin access required")
