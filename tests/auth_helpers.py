"""Shared test helper for building a valid bearer token for a Client row, without going
through the real /oauth/token endpoint on every test (one dedicated test exercises that flow
end-to-end — see tests/api/test_oauth_endpoint.py).
"""

from datetime import UTC, datetime, timedelta

from jose import jwt

from app.core.config import get_settings
from app.db.models import Client


def bearer_header(client: Client) -> dict[str, str]:
    settings = get_settings()
    now = datetime.now(UTC)
    payload = {"sub": str(client.id), "iat": now, "exp": now + timedelta(minutes=30)}
    token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return {"Authorization": f"Bearer {token}"}
