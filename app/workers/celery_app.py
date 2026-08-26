"""Celery application instance. Task modules (poller, outbox_dispatcher) register here."""

from celery import Celery

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "oscu_gateway",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.workers.poller"],
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
    },
)
