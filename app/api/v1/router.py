"""Aggregates all /v1 endpoint routers. Most are stubs until their build phase lands."""

from fastapi import APIRouter

from app.api.v1.endpoints import (
    branches,
    codes,
    device,
    health,
    imports,
    items,
    purchases,
    sales,
    stock,
)

api_router = APIRouter(prefix="/v1")

api_router.include_router(health.router)
api_router.include_router(device.router, prefix="/devices", tags=["devices"])
api_router.include_router(codes.router, prefix="/codes", tags=["codes"])
api_router.include_router(items.router, prefix="/items", tags=["items"])
api_router.include_router(branches.router, prefix="/branches", tags=["branches"])
api_router.include_router(sales.router, prefix="/sales-invoices", tags=["sales"])
api_router.include_router(purchases.router, prefix="/purchases", tags=["purchases"])
api_router.include_router(stock.router, prefix="/stock", tags=["stock"])
api_router.include_router(imports.router, prefix="/imports", tags=["imports"])
