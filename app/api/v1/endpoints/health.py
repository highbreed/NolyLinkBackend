"""GET /v1/health — liveness/readiness. Gains last-successful-poll-per-resource in Phase 10."""

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
