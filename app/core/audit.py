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
from app.core.metrics import http_request_duration_seconds
from app.core.request_context import request_id_var

logger = logging.getLogger("audit")

REQUEST_ID_HEADER = "X-Request-ID"

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


def _route_template(request: Request) -> str:
    """The matched route's path template (e.g. "/v1/sales-invoices/{external_ref}"), not the
    resolved URL — using the raw path as a metric label would give every distinct invoice
    ref/task code its own label value, an unbounded-cardinality footgun in Prometheus.

    request.scope["route"].path only carries the *locally* matched router's own pattern
    (e.g. "/health", not "/v1/health") once routes are included via nested APIRouters, so
    this substitutes each resolved path_param's value back into "{name}" against the full
    resolved URL instead — robust to routing internals, and to how deeply routers are
    nested. Only meaningful after routing runs, i.e. after call_next().
    """
    path = request.url.path
    for name, value in request.path_params.items():
        path = path.replace(str(value), f"{{{name}}}", 1)
    return path


class AuditLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        token = request_id_var.set(request_id)

        try:
            start = time.monotonic()
            body_bytes = await request.body()  # Starlette caches this; handlers still see it.

            response = await call_next(request)
            duration_seconds = time.monotonic() - start
            response.headers[REQUEST_ID_HEADER] = request_id

            http_request_duration_seconds.labels(
                method=request.method,
                path=_route_template(request),
                status_code=response.status_code,
            ).observe(duration_seconds)

            redacted_body: Any = None
            if body_bytes:
                try:
                    redacted_body = _redact(json.loads(body_bytes))
                except json.JSONDecodeError:
                    redacted_body = "<non-json body, not logged>"

            # Logged inside the try block, before the contextvar reset below, so this line
            # (and anything else logged during the request) still carries request_id.
            logger.info(
                "audit",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": round(duration_seconds * 1000, 2),
                    "client_id": _client_id_from_request(request),
                    "body": redacted_body,
                },
            )
            return response
        finally:
            request_id_var.reset(token)
