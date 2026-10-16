"""Aggregates all /v1 endpoint routers. Most are stubs until their build phase lands."""

from fastapi import APIRouter

from app.api.v1.endpoints import (
    admin_dashboard,
    admin_reference_data,
    billing,
    branches,
    codes,
    customers,
    device,
    health,
    imports,
    item_classes,
    items,
    monitoring,
    notices,
    purchases,
    sales,
    stock,
    vscu,
    vscu_reference_data,
    vscu_reports,
    vscu_sales_admin,
    vscu_stock_admin,
)

api_router = APIRouter(prefix="/v1")

api_router.include_router(health.router)
api_router.include_router(device.router, prefix="/devices", tags=["devices"])
api_router.include_router(customers.router, prefix="/customers", tags=["customers"])
api_router.include_router(admin_dashboard.router, prefix="/admin", tags=["admin"])
api_router.include_router(
    admin_reference_data.router, prefix="/admin/reference-data", tags=["admin"]
)
api_router.include_router(monitoring.router, prefix="/monitoring", tags=["monitoring"])
api_router.include_router(billing.router, prefix="/billing", tags=["billing"])
api_router.include_router(codes.router, prefix="/codes", tags=["codes"])
api_router.include_router(item_classes.router, prefix="/item-classes", tags=["item-classes"])
api_router.include_router(items.router, prefix="/items", tags=["items"])
api_router.include_router(branches.router, prefix="/branches", tags=["branches"])
api_router.include_router(sales.router, prefix="/sales-invoices", tags=["sales"])
api_router.include_router(purchases.router, prefix="/purchases", tags=["purchases"])
api_router.include_router(stock.router, prefix="/stock", tags=["stock"])
api_router.include_router(imports.router, prefix="/imports", tags=["imports"])
api_router.include_router(notices.router, prefix="/notices", tags=["notices"])
api_router.include_router(vscu.router, prefix="/vscu", tags=["vscu"])
api_router.include_router(vscu_reference_data.router, prefix="/vscu", tags=["vscu"])
api_router.include_router(vscu_reports.router, prefix="/vscu", tags=["vscu"])
api_router.include_router(vscu_sales_admin.router, prefix="/vscu", tags=["vscu"])
api_router.include_router(vscu_stock_admin.router, prefix="/vscu", tags=["vscu"])
