"""FastAPI app entrypoint: instantiates the app, wires routers, registers startup events."""

from fastapi import FastAPI

from app.api.oauth import router as oauth_router
from app.api.v1.router import api_router
from app.core.audit import AuditLogMiddleware
from app.core.config import get_settings

settings = get_settings()

app = FastAPI(title="OSCU Gateway", version="0.1.0")

app.add_middleware(AuditLogMiddleware)

app.include_router(api_router)
app.include_router(oauth_router, prefix="/oauth", tags=["oauth"])
