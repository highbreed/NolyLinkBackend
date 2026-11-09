"""Request/response audit logging with PII-aware redaction (spec §15: "All inbound payloads
logged (with PII/tax data access-controlled) for audit trail required by KRA compliance").

Logs method/path/status/duration/client_id (best-effort, decoded from the bearer token
without verifying it — this is for log attribution only, not an auth check) and a redacted
copy of the JSON body to stdout (`logger.info("audit", ...)`), AND persists the same
information plus the response body to client_call_log (app/db/models.py) — "what did a
client actually send us, and what did we send back", queryable from the admin portal instead
of living only in transient log output. The DB write is best-effort: a failure there is
logged and swallowed, never allowed to break the actual response.

Headers aren't logged at all, so Authorization/X-Admin-Api-Key never need redacting.
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
from app.db import session as db_session_module
from app.db.models import ClientCallLog

logger = logging.getLogger("audit")
# Deliberately a different logger than "audit" above: tests assert exactly one "audit"-named
# record per request (the stdout summary line), so a persistence failure logs here instead,
# never muddying that count.
_persist_logger = logging.getLogger("audit.persist")

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

            # Drain the response body so it can be logged/persisted, then rebuild an
            # equivalent Response — downstream (the ASGI server) needs a normal body, not a
            # consumed-once iterator.
            response_body_bytes = b"".join(
                [chunk async for chunk in response.body_iterator]  # type: ignore[attr-defined]
            )
            response = Response(
                content=response_body_bytes,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
            )

            http_request_duration_seconds.labels(
                method=request.method,
                path=_route_template(request),
                status_code=response.status_code,
            ).observe(duration_seconds)

            redacted_request_body = _parse_and_redact(body_bytes)
            redacted_response_body = _parse_and_redact(response_body_bytes)
            client_id = _client_id_from_request(request)
            duration_ms = round(duration_seconds * 1000, 2)

            # Logged inside the try block, before the contextvar reset below, so this line
            # (and anything else logged during the request) still carries request_id.
            logger.info(
                "audit",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": duration_ms,
                    "client_id": client_id,
                    "body": redacted_request_body,
                },
            )

            await _persist_call_log(
                request_id=request_id,
                client_id=client_id,
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                duration_ms=int(duration_ms),
                request_body=redacted_request_body,
                response_body=redacted_response_body,
            )

            return response
        finally:
            request_id_var.reset(token)


def _parse_and_redact(body_bytes: bytes) -> Any:
    if not body_bytes:
        return None
    try:
        return _redact(json.loads(body_bytes))
    except json.JSONDecodeError:
        return "<non-json body, not logged>"


async def _persist_call_log(
    *,
    request_id: str,
    client_id: str | None,
    method: str,
    path: str,
    status_code: int,
    duration_ms: int,
    request_body: Any,
    response_body: Any,
) -> None:
    """Best-effort: a DB failure here must never break the actual response the client gets.

    Resolves app.db.session.AsyncSessionLocal via module attribute lookup, not a direct
    `from ... import AsyncSessionLocal`, specifically so tests can monkeypatch the module
    attribute to a test-DB-bound session factory (see tests/conftest.py's
    _patch_async_session_local) — this middleware runs on every request regardless of which
    endpoint is under test, unlike outbox_dispatcher's _dispatch_pending, which tests call
    directly with an explicit session_factory override instead.
    """
    try:
        parsed_client_id = uuid.UUID(client_id) if client_id else None
        async with db_session_module.AsyncSessionLocal() as db:
            db.add(
                ClientCallLog(
                    request_id=request_id,
                    client_id=parsed_client_id,
                    method=method,
                    path=path,
                    status_code=status_code,
                    duration_ms=duration_ms,
                    request_body=request_body,
                    response_body=response_body,
                )
            )
            await db.commit()
    except Exception:
        _persist_logger.exception(
            "Failed to persist client_call_log row (request_id=%s)", request_id
        )
