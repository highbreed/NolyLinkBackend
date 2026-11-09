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
    include=[
        "app.workers.poller",
        "app.workers.outbox_dispatcher",
        "app.workers.vscu_poller",
        "app.workers.cleanup",
        "app.workers.alert_evaluator",
    ],
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
        "refresh-item-classes": {
            "task": "poller.refresh_item_classes",
            "schedule": settings.kra_poll_interval_item_classes_seconds,
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
        "cleanup-client-call-log": {
            "task": "cleanup.client_call_log",
            "schedule": 24 * 60 * 60,
        },
        "evaluate-alert-rules": {
            "task": "alert_evaluator.evaluate_alert_rules",
            "schedule": 5 * 60,
        },
        # VSCU pollers reuse the exact same intervals as their OSCU counterparts — same
        # underlying KRA resources, just a different transport per Branch.device_type.
        "vscu-refresh-codes": {
            "task": "vscu_poller.refresh_codes",
            "schedule": settings.kra_poll_interval_codes_seconds,
        },
        "vscu-refresh-item-classes": {
            "task": "vscu_poller.refresh_item_classes",
            "schedule": settings.kra_poll_interval_item_classes_seconds,
        },
        "vscu-refresh-import-items": {
            "task": "vscu_poller.refresh_import_items",
            "schedule": settings.kra_poll_interval_import_items_seconds,
        },
        "vscu-refresh-purchase-sales": {
            "task": "vscu_poller.refresh_purchase_sales",
            "schedule": settings.kra_poll_interval_purchase_sales_seconds,
        },
        "vscu-refresh-stock-moves": {
            "task": "vscu_poller.refresh_stock_moves",
            "schedule": settings.kra_poll_interval_stock_moves_seconds,
        },
        "vscu-refresh-notices": {
            "task": "vscu_poller.refresh_notices",
            "schedule": settings.kra_poll_interval_notices_seconds,
        },
    },
)
