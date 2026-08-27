# Observability

## Structured logging

Every log line is JSON (`app/core/logging.py`, via `python-json-logger`), with a
`request_id` field on every record emitted during a request's handling — not just the audit
line — via a `logging.LogRecordFactory`, not a `logging.Filter` (a Filter attached to one
logger doesn't apply to records that originate on a different logger during propagation; the
factory runs for every record regardless of origin). `configure_logging()` runs at process
startup for both the API (`app/main.py`) and Celery workers/beat (`app/workers/celery_app.py`).

`AuditLogMiddleware` (`app/core/audit.py`) logs one line per request — method, path, status,
duration, `client_id` (best-effort, decoded from the bearer token without verifying it — for
log attribution only), and a **redacted** copy of the JSON body (spec §15: "PII/tax data
access-controlled"). `REDACTED_KEYS` in that module is the exact field-name allowlist —
`cmcKey`, `pwd`, `client_secret`, and customer PII fields (`custTin`/`cust_tin`,
`custNm`/`cust_nm`, `email`, `tel_no`, `adrs`, ...). Headers are never logged at all, so
`Authorization`/`X-Admin-Api-Key` never need redacting.

### Request-ID correlation

Every response carries an `X-Request-ID` header — echoed back if the caller sent one,
generated (`uuid4`) otherwise. Use it to correlate a client's own logs with the gateway's:
grep any log line for that ID (API and worker logs both, since Celery tasks triggered from a
request would carry the same ID if propagated — pollers/outbox_dispatcher run outside any
request context, so their logs have `request_id: null`, which is expected, not a bug).

## Prometheus metrics

`GET /metrics` (unversioned, unauthenticated — the Prometheus scraper hits this directly, not
an API client) renders the process's metric registry. Two gauges are DB-backed and refreshed
at scrape time rather than on a timer, so they're never stale between scrapes:
`outbox_queue_depth` and `kra_sync_lag_seconds`.

| Metric | Type | Labels | What it means |
|---|---|---|---|
| `http_request_duration_seconds` | Histogram | `method`, `path`, `status_code` | Gateway-side request latency. `path` is the **route template** (e.g. `/v1/sales-invoices/{external_ref}`), not the resolved URL — using the raw path would give every distinct invoice ref/task code its own label value, an unbounded-cardinality footgun in Prometheus. |
| `kra_calls_total` | Counter | `endpoint`, `result_cd` | Every KRA adapter call (spec §5), by which KRA endpoint and which `resultCd` it returned (spec §10) — `"000"` is success, everything else is one of the `KraError` subclasses. |
| `outbox_queue_depth` | Gauge | `status` | Rows in `outbox` (spec §14) by status — `PENDING`, `RETRYING`, `SENT`, `FAILED`. |
| `kra_sync_lag_seconds` | Gauge | `resource` | Seconds since the last successful poll for that resource (`codes`, `notices`, `import_items`, `purchase_sales`, `stock_moves` — spec §11), across all branches. |

Standard `prometheus_client` process/GC metrics (`process_*`, `python_gc_*`) are included too
via the default registry — not listed above, but present in the scrape output.

### PromQL queries

**p95 request latency, per route** (spec §19's NFR: "p95 < 3s for synchronous
`/v1/sales-invoices` calls"):
```promql
histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket[5m])) by (le, path))
```

**Error rate by KRA resultCd, last 5 minutes** — anything other than `000`/`001` is worth
alerting on if it spikes:
```promql
sum(rate(kra_calls_total{result_cd!~"000|001"}[5m])) by (endpoint, result_cd)
```

**KRA call success ratio, per endpoint**:
```promql
sum(rate(kra_calls_total{result_cd="000"}[5m])) by (endpoint)
/
sum(rate(kra_calls_total[5m])) by (endpoint)
```

**Outbox backlog** (spec §14's backoff schedule tops out at 1h — a growing `RETRYING`/
`PENDING` count over that window means dispatch isn't keeping up, not just normal backoff):
```promql
outbox_queue_depth{status=~"PENDING|RETRYING"}
```

**Permanently failed outbox rows** (spec §14: "then alert" — this is that alert's source
metric; page on this being non-zero, since these need manual intervention, not a retry):
```promql
outbox_queue_depth{status="FAILED"} > 0
```

**Sync lag exceeding its own polling interval** (a poller that should run every 15 minutes
but hasn't succeeded in over 30 is failing silently, not just running behind):
```promql
kra_sync_lag_seconds{resource="import_items"} > 1800
kra_sync_lag_seconds{resource="purchase_sales"} > 1800
kra_sync_lag_seconds{resource="stock_moves"} > 1800
kra_sync_lag_seconds{resource="notices"} > 7200
kra_sync_lag_seconds{resource="codes"} > 43200
```
(Thresholds are 2x each resource's configured poll interval from `app/core/config.py` —
`KRA_POLL_INTERVAL_*_SECONDS` — adjust if you change those.)

**Request volume by status code, per client** — not exposed as a metric label today
(`client_id` isn't on `http_request_duration_seconds`, only in the audit log line, to keep
that histogram's cardinality bounded by route × method × status rather than growing per
client); use `kra_call_log`/audit logs for per-client breakdowns instead.

## `GET /v1/health`

Returns `{"status": "ok", "last_successful_poll": {"<resource>": "<ISO 8601 timestamp>", ...}}`
— the most recent successful poll per resource, across all branches (not broken out
per-branch, which would grow unbounded with the branch count and isn't needed for a liveness
check). A resource that has never synced successfully simply doesn't appear in the dict yet.

## Grafana

No dashboard JSON is checked in — the PromQL above is enough to build panels directly, and a
dashboard JSON would need to encode assumptions (panel layout, refresh interval, which
thresholds are "alert-worthy" for a specific deployment) that are better left to whoever
actually operates this in production than guessed here.
