"""Records an admin action to admin_audit_log — see that model's docstring for why this
supplements rather than replaces the existing logger.info() calls across admin.py/billing.py/
customers.py/device.py.

Always call this BEFORE the endpoint's own `await db.commit()`, never with a separate commit
of its own — it must land atomically with the action it records, or never at all if that
action's own commit fails.
"""

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AdminAuditLog

# require_admin (app/core/admin_auth.py) returns "api-key" or "user:<username>" — this is the
# one place that string gets parsed back apart, so admin_user_email means None (break-glass
# key) or a username, never anything else.
_API_KEY_ACTOR = "api-key"
_USER_ACTOR_PREFIX = "user:"


def _admin_user_email_from_actor(actor: str) -> str | None:
    if actor == _API_KEY_ACTOR:
        return None
    return actor.removeprefix(_USER_ACTOR_PREFIX)


async def record_admin_action(
    db: AsyncSession,
    *,
    actor: str,
    action: str,
    target_type: str,
    target_id: str,
    before: dict[str, object] | None = None,
    after: dict[str, object] | None = None,
    reason: str | None = None,
    request: Request | None = None,
) -> None:
    db.add(
        AdminAuditLog(
            admin_user_email=_admin_user_email_from_actor(actor),
            action=action,
            target_type=target_type,
            target_id=target_id,
            before_state=before,
            after_state=after,
            reason=reason,
            ip_address=request.client.host if request is not None and request.client else None,
            request_id=request.headers.get("X-Request-ID") if request is not None else None,
        )
    )
    await db.flush()
