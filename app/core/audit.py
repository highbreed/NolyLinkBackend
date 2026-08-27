"""Request/response audit logging with PII-aware redaction (spec §15: "All inbound payloads
logged (with PII/tax data access-controlled) for audit trail required by KRA compliance").

Logs method/path/status/duration/client_id (best-effort, decoded from the bearer token
without verifying it — this is for log attribution only, not an auth check) and a redacted
copy of the JSON body. Headers aren't logged at all, so Authorization/X-Admin-Api-Key never
need redacting. Full structured JSON log output (matching whatever schema Phase 10's
observability work settles on) is out of scope here — this uses stdlib logging with `extra`,
which a later JSON formatter can pick up without changing this module.
"""

import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from jose import jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import get_settings

logger = logging.getLogger("audit")

REDACTED = "***REDACTED***"

# Exact field names (KRA's camelCase and the client DTOs' snake_case) carrying credentials
# or PII/tax-identifying data that must never reach the audit log in plaintext.
REDACTED_KEYS = {
    "cmcKey",
    "pwd",
    "client_secret",
    "custTin",
    "custNm",
    "custMblNo",
    "customer_pin",
    "customer_name",
    "cust_tin",
    "cust_nm",
    "tel_no",
    "email",
    "adrs",
}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: (REDACTED if k in REDACTED_KEYS else _redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _client_id_from_request(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    token = auth[len("Bearer ") :]
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret_key,
            algorithms=[settings.jwt_algorithm],
            options={"verify_exp": False},
        )
        sub = payload.get("sub")
        return str(uuid.UUID(sub)) if sub else None
    except Exception:
        return None


class AuditLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        start = time.monotonic()
        body_bytes = await request.body()  # Starlette caches this; handlers still see it.

        response = await call_next(request)
        duration_ms = round((time.monotonic() - start) * 1000, 2)

        redacted_body: Any = None
        if body_bytes:
            try:
                redacted_body = _redact(json.loads(body_bytes))
            except json.JSONDecodeError:
                redacted_body = "<non-json body, not logged>"

        logger.info(
            "audit",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": duration_ms,
                "client_id": _client_id_from_request(request),
                "body": redacted_body,
            },
        )
        return response
