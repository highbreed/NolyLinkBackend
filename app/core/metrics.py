"""Prometheus metric definitions (spec's Phase 10 task list): request latency histograms,
KRA call success/failure counters by resultCd, outbox queue depth, sync lag per resource.

A single shared registry (the prometheus_client default) backs all of these; GET /metrics
(app/api/metrics.py) renders it, refreshing the two DB-backed gauges at scrape time rather
than on a timer, so they're never stale between scrapes.
"""

from prometheus_client import Counter, Gauge, Histogram

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    labelnames=["method", "path", "status_code"],
)

kra_calls_total = Counter(
    "kra_calls_total",
    "KRA adapter calls by endpoint and resultCd (spec §10)",
    labelnames=["endpoint", "result_cd"],
)

outbox_queue_depth = Gauge(
    "outbox_queue_depth",
    "Rows in the outbox table by status (spec §14)",
    labelnames=["status"],
)

kra_sync_lag_seconds = Gauge(
    "kra_sync_lag_seconds",
    "Seconds since the last successful poll, per resource (spec §11)",
    labelnames=["resource"],
)

vscu_requests_total = Counter(
    "vscu_requests_total",
    "VSCU integration layer calls by endpoint and resultCd",
    labelnames=["endpoint", "result_cd"],
)

vscu_request_duration_seconds = Histogram(
    "vscu_request_duration_seconds",
    "VSCU local-app call latency in seconds",
    labelnames=["endpoint"],
)
