# OSCU Gateway

FastAPI integration gateway between client ERP/POS systems (SAP, other ERPs, POS) and KRA's
eTIMS OSCU API. See [`KRA_OSCU_FastAPI_Integration_Spec.md`](../KRA_OSCU_FastAPI_Integration_Spec.md)
in the repo root for the full technical specification this project implements, and
[`docs/sap_integration_guide.md`](sap_integration_guide.md) for the client-facing DTO
contract and sample payloads if you're the one integrating a source system against this
gateway rather than building it.

Other docs: [`api_reference.md`](api_reference.md) is the exhaustive endpoint-by-endpoint
reference (every route, auth scheme, request/response shape, error code);
[`client_onboarding_and_monitoring.md`](client_onboarding_and_monitoring.md) is a narrative
walkthrough of onboarding a new client end to end and monitoring them afterward, naming the
exact endpoint used at each step.

## Status

All 11 build phases are implemented — device init, code-table caching, item/branch master
data, sales invoices (sync), purchases/stock/imports/notices (async outbox), client
authN/Z + rate limiting, and observability. See each phase's commit for what it added.

**What's still genuinely open**, not just unfinished: no real KRA sandbox credentials have
ever been provided to this build, so every sandbox-dependent claim in this document and in
`KRA_OSCU_FastAPI_Integration_Spec.md` §20 is verified against a mocked KRA (`respx`), not
the live API. See [`docs/open_questions_resolved.md`](open_questions_resolved.md) for the
specific open items and what would close each one out.

## Running locally (Docker)

```bash
cp .env.example .env
make dev      # docker-compose up --build: app + postgres + redis (+ celery worker)
```

Then `curl localhost:8000/v1/health` should return `{"status": "ok", "last_successful_poll": {}}`.

```bash
make test       # run pytest inside the app container
make migrate     # alembic upgrade head
make lint        # ruff
make fmt          # black
make typecheck    # mypy
```

Note: this Docker path hasn't actually been run in the environment this gateway was built
in (no working Docker socket there) — everything above was instead verified against a
Python 3.11 venv and real local Postgres/Redis instances. If `make dev` doesn't come up
cleanly, that's the first thing to check.

## Running locally (no Docker)

```bash
python3.11 -m venv venv
./venv/bin/pip install -e ".[dev]"
```

Point `DATABASE_URL` at any disposable Postgres you have (local install, `pg_ctlcluster`,
etc.) — `app/db/models.py`'s Postgres-specific column types (`ARRAY`, `JSONB`, `UUID`) mean
SQLite won't work as a stand-in. Point `REDIS_URL`/`CELERY_BROKER_URL`/`CELERY_RESULT_BACKEND`
at any Redis instance (rate limiting and Celery both need one). Then:

```bash
./venv/bin/alembic upgrade head
./venv/bin/uvicorn app.main:app --reload
./venv/bin/pytest
./venv/bin/ruff check app tests && ./venv/bin/mypy app && ./venv/bin/black --check app tests
```

To run the background workers (pollers, outbox dispatcher — see spec §11/§14):
```bash
./venv/bin/celery -A app.workers.celery_app worker --beat --loglevel=INFO
```

### Test database

Tests never run against `DATABASE_URL` — always against `Settings.resolved_test_database_url`
(that database's name + `_test`, or `TEST_DATABASE_URL` if you set it explicitly), so a test
run's `create_all`/`drop_all` can never touch real data. The role needs `CREATEDB`, or create
it once yourself:
```sql
CREATE DATABASE <your db name>_test OWNER <your db role>;
```
Tests also hit the real `REDIS_URL` (rate limiting, Celery) — there's no separate test Redis
instance, since keys are namespaced by randomly-generated client/branch UUIDs per test and
carry short TTLs, so collisions with real data aren't a practical risk.

## Pointing at KRA sandbox vs production

Set `KRA_ENV=sandbox` or `KRA_ENV=production` in `.env`. The gateway picks `KRA_BASE_URL_SBX`
or `KRA_BASE_URL_PROD` accordingly (see `app/core/config.py`). Always develop and test
against sandbox; production credentials and cutover are a deliberate, separate step (see the
build prompt's "Ask before assuming" section, and §20 of the spec).

## Onboarding a new branch (device initialization)

One-time, per `(tin, bhfId)` — spec §12. This is deliberately **not** reachable by SAP/ERP
clients: it's gated behind `ADMIN_API_KEY` (an `X-Admin-Api-Key` header), a provisional
mechanism Phase 9's OAuth2 system doesn't replace, since this endpoint's whole point is that
ordinary clients must never touch it.

```bash
curl -X POST http://localhost:8000/v1/devices/initialize \
  -H "X-Admin-Api-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"branch_code": "NAIROBI-HQ", "tin": "A123456789Z", "bhf_id": "00"}'
```

This calls KRA's `/selectInitOsdcInfo` and persists the returned `cmcKey` (encrypted at
rest — see `app/db/encrypted_types.py`) keyed by your internal `branch_code`. The response
never includes `cmcKey` itself (see the automated check for this in
`tests/api/test_device_endpoint.py`); every other endpoint references the branch by
`branch_code` from then on, never `tin`/`bhfId`/`cmcKey` directly.

**OSCU vs VSCU / multi-tenant hosts.** VSCU devices use this exact same request/response
contract — only the host differs. Add `"device_type": "VSCU"` to the request above and it
resolves to `KRA_BASE_URL_VSCU_SBX`/`_PROD` for every call this branch ever makes (fails with
422 if that's not configured yet). If a specific branch/tenant needs a host that isn't either
global default — e.g. a customer's own dedicated KRA gateway — pin it directly with
`"kra_base_url_override": "https://..."` instead; that branch always uses exactly that host
from then on, regardless of `device_type` or later changes to the global settings. Every
branch is independent, so different clients (different `Client` rows, each scoped to their
own branches via `allowed_branch_ids`) can freely mix OSCU, VSCU, and custom-host branches on
one running gateway.

If KRA returns `902` (already installed) and a local record already exists for that
`(tin, bhfId)`, the call is treated as idempotent and returns the existing record. If `902`
comes back with **no** local record, the gateway refuses to guess — it raises rather than
silently re-initializing a fiscal device — and this needs manual investigation (see
`app/services/cmc_key_manager.py`).

## Onboarding a new client

Clients authenticate via OAuth2 client-credentials (spec §15) — `clients.id` doubles as the
OAuth2 `client_id`, and `clients.client_secret_hash` is a sha256 hash of the secret. There's
no client-provisioning endpoint (out of scope for the phases this build covered), so create
the row directly:

```sql
INSERT INTO clients (id, name, client_secret_hash, allowed_branch_ids)
VALUES (
    gen_random_uuid(),
    'SAP-PROD',
    -- sha256 of the raw secret you're handing to the client team — generate with:
    --   python -c "import hashlib; print(hashlib.sha256(b'<raw-secret>').hexdigest())"
    '<sha256 hex digest>',
    ARRAY['<branch uuid from the branches table>']::uuid[]
);
```

`allowed_branch_ids` is spec §15's per-client branch allow-listing — a client with valid
credentials can still only act on the branches explicitly listed here (see
`app/services/client_auth.py`'s `get_authorized_branch`). Hand the client team the `id` you
generated (their OAuth2 `client_id`) and the raw secret (never store that raw value
yourself — only its hash).

## Client billing (subscriptions and invoices)

This gateway's own tenant billing — entirely separate from KRA, and off (`BILLING_ENFORCEMENT_ENABLED=false`)
by default. See [`app/services/billing_service.py`](../app/services/billing_service.py) for
the enforcement rules. Internal tracking only: an admin marks invoices paid and subscriptions
suspended directly; there's no payment processor integration (no real credentials for one
were provided, same policy this build followed for KRA sandbox access throughout).

Enabling enforcement blocks any client with no subscription on file, so **create a
subscription for every existing client before flipping it on**:

```bash
curl -X PUT http://localhost:8000/v1/billing/subscriptions/<client id> \
  -H "X-Admin-Api-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"plan_name": "STANDARD", "current_period_end": "2026-12-31T00:00:00Z"}'
```

A client keeps working for `BILLING_GRACE_PERIOD_DAYS` (default 7, overridable per-subscription
via `grace_period_days`) past `current_period_end` before requests start getting `402`. Record
an invoice and mark it paid (optionally renewing the subscription in the same call):

```bash
curl -X POST http://localhost:8000/v1/billing/clients/<client id>/invoices \
  -H "X-Admin-Api-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"period_start": "2026-01-01T00:00:00Z", "period_end": "2026-02-01T00:00:00Z", "amount": "5000.00"}'

curl -X POST http://localhost:8000/v1/billing/invoices/<invoice id>/mark-paid \
  -H "X-Admin-Api-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"extend_subscription_to": "2026-02-01T00:00:00Z"}'
```

`POST /v1/billing/subscriptions/{client_id}/suspend` (with a `reason`) blocks a client
immediately regardless of its paid-through date — e.g. for a chargeback — and
`.../reactivate` lifts it. Renewing a subscription's period never lifts an explicit
suspension on its own; reactivate is a separate, deliberate step.

The client then requests a token:
```bash
curl -X POST http://localhost:8000/oauth/token \
  -d "grant_type=client_credentials&client_id=<the id>&client_secret=<the raw secret>"
```
which returns a short-lived (`JWT_ACCESS_TOKEN_EXPIRE_MINUTES`, default 30) bearer token to
send as `Authorization: Bearer <token>` on every other request.

## Registering an item and submitting a sale (smoke test)

Once a branch and a client both exist:

```bash
TOKEN=<from /oauth/token above>

curl -X POST http://localhost:8000/v1/items \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "branch_code": "NAIROBI-HQ", "sku": "BREAD-400G", "name": "Bread 400g",
    "item_classification_cd": "5020150101", "unit_of_measure": "BA",
    "packaging_unit": "NT", "product_type": "FINISHED_PRODUCT",
    "tax_category": "STANDARD16", "unit_price": "100.00"
  }'

curl -X POST http://localhost:8000/v1/sales-invoices \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{
    "branch_code": "NAIROBI-HQ", "external_invoice_ref": "SAP-BILLDOC-0001",
    "payment_method": "CASH", "invoice_date": "2026-01-01",
    "lines": [{"sku": "BREAD-400G", "description": "Bread 400g", "quantity": "2",
               "unit_of_measure": "BA", "unit_price": "100.00", "tax_category": "STANDARD16"}]
  }'
```

The sales response includes `kra_rcpt_sign`/`kra_cur_rcpt_no`/etc. — the fiscal receipt
proof a compliant receipt is printed with (spec §6.6). Resubmitting the same
`external_invoice_ref` returns the same cached response instead of calling KRA again
(spec §13).

## Observability

See [`docs/observability.md`](observability.md) — structured JSON logs with request-ID
correlation, Prometheus metrics at `GET /metrics`, and `GET /v1/health`'s per-resource
last-successful-poll report.

## Project layout

```
app/
  api/            oauth.py (/oauth/token), metrics.py (/metrics), v1/ (versioned endpoints)
  core/           config, security (cmcKey encryption), audit logging, metrics, rate limiting
  db/             SQLAlchemy models, session, encrypted column type
  schemas/kra/    1:1 Pydantic mirrors of KRA's JSON contracts (spec §6)
  schemas/client/ simplified inbound DTOs for SAP/other ERPs (spec §9.2)
  services/       business logic — one module per domain, plus the KRA adapter and mappers
  workers/        Celery app, pollers, outbox dispatcher
tests/
  api/            endpoint-level tests (auth, status codes, response shape)
  services/       business-logic tests (mapping, sync, idempotency, encryption, ...)
  db/             model round-trip tests
  integration/    live-sandbox tests, skipped by default (no credentials)
  fixtures/       golden JSON fixtures for the KRA schema layer
```
