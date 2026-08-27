"""Celery application instance. Task modules (poller, outbox_dispatcher) register here."""

from celery import Celery

from app.core.config import get_settings
from app.core.logging import configure_logging

settings = get_settings()
configure_logging()

celery_app = Celery(
    "oscu_gateway",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.workers.poller", "app.workers.outbox_dispatcher"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    beat_schedule={
        "refresh-code-lists": {
            "task": "poller.refresh_code_lists",
            "schedule": settings.kra_poll_interval_codes_seconds,
        },
        "refresh-import-items": {
            "task": "poller.refresh_import_items",
            "schedule": settings.kra_poll_interval_import_items_seconds,
        },
        "refresh-purchase-sales": {
            "task": "poller.refresh_purchase_sales",
            "schedule": settings.kra_poll_interval_purchase_sales_seconds,
        },
        "refresh-stock-moves": {
            "task": "poller.refresh_stock_moves",
            "schedule": settings.kra_poll_interval_stock_moves_seconds,
        },
        "refresh-notices": {
            "task": "poller.refresh_notices",
            "schedule": settings.kra_poll_interval_notices_seconds,
        },
        "dispatch-outbox": {
            "task": "outbox_dispatcher.dispatch_pending",
            "schedule": settings.kra_outbox_dispatch_interval_seconds,
        },
    },
)
