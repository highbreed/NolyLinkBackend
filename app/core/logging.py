"""Structured JSON logging setup (tech baseline: "structured logging (JSON)").

Call configure_logging() once at process startup — app/main.py for the API process, and
app/workers/celery_app.py for Celery workers/beat, so log output is JSON everywhere, not
just in the API.
"""

import logging

from pythonjsonlogger import jsonlogger

from app.core.config import get_settings
from app.core.request_context import install_request_id_log_factory


def configure_logging() -> None:
    settings = get_settings()
    install_request_id_log_factory()

    handler = logging.StreamHandler()
    handler.setFormatter(
        jsonlogger.JsonFormatter(  # type: ignore[no-untyped-call]
            "%(asctime)s %(name)s %(levelname)s %(message)s %(request_id)s"
        )
    )

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)
