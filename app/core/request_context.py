"""Request-ID correlation (tech baseline: "structured JSON logging; correlation/request IDs
on every request"). A ContextVar rather than passing the ID explicitly means every log line
emitted anywhere during a request's handling — not just AuditLogMiddleware's own line — can
carry the same ID, without threading it through every function call.

Uses a LogRecordFactory rather than a logging.Filter: a Filter attached to a logger only runs
for records *originating* on that exact logger (Logger.handle() calls self.filter() once, on
the logger the message was logged through — ancestor loggers visited during propagation don't
re-run their own .filters, only their directly-attached handlers' filters). A record logged
via logging.getLogger("audit") would never see a filter added to the root logger. The factory
runs for every LogRecord at creation time, before any of that, so it works regardless of which
logger created the record or which handler(s) eventually process it — including pytest's
caplog, which attaches its own handler directly to the root logger.
"""

import contextvars
import logging

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)

_original_factory = logging.getLogRecordFactory()


def _record_factory(*args: object, **kwargs: object) -> logging.LogRecord:
    record = _original_factory(*args, **kwargs)
    record.request_id = request_id_var.get()
    return record


def install_request_id_log_factory() -> None:
    logging.setLogRecordFactory(_record_factory)
