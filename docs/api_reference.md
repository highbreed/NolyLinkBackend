# OSCU Gateway — API Reference

Full reference for every HTTP endpoint this gateway exposes. For a narrower, task-oriented
walkthrough see [`sap_integration_guide.md`](sap_integration_guide.md) (client integrators)
and [`client_onboarding_and_monitoring.md`](client_onboarding_and_monitoring.md) (ops/admin
running the gateway). This document covers the same surface exhaustively, endpoint by endpoint.

Base URL: whatever `uvicorn`/your deployment binds to (e.g. `http://localhost:8000`). All
`/v1/*` paths are versioned; `/oauth/token` and `/metrics` are not (standard convention for a
token endpoint and a Prometheus scrape target).

## Authentication summary

| Scheme | Header | Used by |
|---|---|---|
| **Client bearer (JWT)** | `Authorization: Bearer <token>` | Every `/v1/*` endpoint except `/v1/health` and `/v1/devices/*` |
| **Admin key** | `X-Admin-Api-Key: <key>` | `/v1/devices/initialize`, all of `/v1/billing/*`, all of `/admin/*` except `/admin/login` itself |
| **Admin bearer (JWT)** | `Authorization: Bearer <token>` | Same routes as the admin key — either credential grants identical access, see [Admin Login](#admin-portal-login) |
| **None** | — | `/oauth/token`, `/admin/login` (the grant exchanges themselves), `/v1/health`, `/metrics` |

Client bearer tokens are obtained from `POST /oauth/token` (below) and expire after
`JWT_ACCESS_TOKEN_EXPIRE_MINUTES` (default 30). There is no refresh grant — request a new
token with the same `client_id`/`client_secret` when a call returns `401`.

Every client bearer request also passes through, in order: **billing enforcement**
(`app/services/billing_service.py` — `402` if the client's subscription is blocked, see
[Billing](#admin--billing) below) then **rate limiting**
(`app/services/rate_limit.py` — `429` past `CLIENT_RATE_LIMIT_PER_MINUTE`, default 120/min).
Both apply before any endpoint-specific logic runs.

## Common error shapes

| Status | Meaning | Applies to |
|---|---|---|
| `401` | Missing/invalid/expired bearer token, or bad admin key on a client-bearer route | All authenticated routes |
| `402` | Client's billing subscription is blocked (no subscription on file, suspended, cancelled, or past its grace period) | All client-bearer routes |
| `403` | Admin key missing/wrong (admin routes), or client not authorized for the requested `branch_code` (client routes) | Admin routes; any route taking `branch_code` |
| `404` | Unknown `branch_code`, unknown resource id, or (GET) resource not found | Most routes |
| `409` | Idempotency conflict (sales invoices) or 902-with-no-local-record (device init) | `POST /v1/sales-invoices`, `POST /v1/devices/initialize` |
| `422` | Body validation error, or a friendly code/sku the gateway couldn't resolve | Routes taking client DTOs |
| `429` | Rate limit exceeded | All client-bearer routes |
| `502` | KRA itself rejected or failed the call — `detail` carries KRA's own `resultCd`/`resultMsg` | Any route that calls KRA synchronously |

---

## Authentication

### `POST /oauth/token`

OAuth2 client-credentials grant (RFC 6749 §4.4). Unversioned, matching standard convention.

**Auth:** none (this *is* the auth exchange).

**Request** — `application/x-www-form-urlencoded`:

| Field | Type | Notes |
|---|---|---|
| `grant_type` | string | Must be `client_credentials` |
| `client_id` | string (UUID) | Issued at client onboarding — see [Billing](#admin--billing) and the onboarding doc |
| `client_secret` | string | The raw secret issued at onboarding (only its sha256 hash is stored server-side) |

**Response** `200`:
```json
{"access_token": "eyJhbGciOi...", "token_type": "bearer", "expires_in": 1800}
```

**Errors:** `400 unsupported_grant_type` (wrong `grant_type`); `401 invalid_client` (unknown
`client_id` or wrong `client_secret`).

---

## Admin Portal Login

Human ops-staff login for the backend admin portal — sits **alongside** the static
`X-Admin-Api-Key`, not in place of it. Either credential grants identical access on every
admin route (`/v1/devices/initialize`, `/v1/billing/*`, `/admin/users/*`). There is no public
signup: the first `AdminUser` has to be created with the static key, and every admin user
management route below is itself admin-gated.

Admin JWTs are signed with the same `JWT_SECRET_KEY` as client OAuth2 tokens but carry a
`"type": "admin"` claim client tokens never have — a client's own bearer token can never be
replayed against an admin route, and vice versa.

### `POST /admin/login`

**Auth:** none (this *is* the login).

**Request:** `{"username": "amina", "password": "..."}`

**Response** `200`:
```json
{"access_token": "eyJhbGciOi...", "token_type": "bearer", "expires_in": 3600}
```
Expiry is `ADMIN_JWT_ACCESS_TOKEN_EXPIRE_MINUTES` (default 60 — longer-lived than client
tokens by default, since this is a human's browser session, not a machine credential
re-requested on every `401`). There is no refresh grant, same as `/oauth/token`.

**Errors:** `401` — unknown username, wrong password, or a deactivated account. The same
error either way, deliberately — distinguishing them would let an attacker enumerate valid
usernames.

### `POST /admin/users`

Creates a new admin portal account. **Admin-gated** (key or an existing admin's token).

**Request:** `username`, `password` (8-72 bytes — 72 is bcrypt's own hard limit), `full_name`
(optional).

**Response** `201`:
```json
{"id": "...", "username": "amina", "full_name": "Amina K.", "is_active": true, "created_at": "...", "last_login_at": null}
```
(Never includes the password or its hash.) **Errors:** `409` — username already taken; `422`
— password outside the 8-72 byte range.

### `GET /admin/users`

Lists every admin user, alphabetically by username. Admin-gated.

### `POST /admin/users/{user_id}/deactivate`

Disables an account immediately — including any bearer token it already holds; every
`Authorization` check re-verifies `is_active` against the database on each request, so this
isn't just "can't log in again," it revokes access mid-session too. Admin-gated.
**Errors:** `404` — unknown `user_id`.

### `POST /admin/users/{user_id}/reactivate`

Restores a deactivated account. Admin-gated. **Errors:** `404` — unknown `user_id`.

---

## Admin — Device Provisioning

Admin-only (`X-Admin-Api-Key` or an admin portal bearer token); never reachable by SAP/ERP clients.

### `POST /v1/devices/initialize`

One-time per `(tin, bhfId)`. Calls KRA's `/selectInitOsdcInfo`, persists the returned `cmcKey`
encrypted at rest, and creates the `branches` row every other endpoint references by
`branch_code` from then on.

**Request:**

| Field | Type | Notes |
|---|---|---|
| `branch_code` | string | Your internal identifier — chosen by you, not KRA |
| `tin` | string, ≤11 | Taxpayer PIN |
| `bhf_id` | string, ≤2 | Branch id |
| `device_type` | `"OSCU"` \| `"VSCU"` | Default `"OSCU"`. Both share the exact same request/response contract — only the host differs. `"VSCU"` resolves to `KRA_BASE_URL_VSCU_SBX`/`_PROD` for every future call on this branch. |
| `kra_base_url_override` | string, optional | Pins this branch to a specific host regardless of `device_type` or later changes to the global settings — e.g. a tenant with its own dedicated KRA gateway |

**Response** `201` (never includes `cmc_key`):
```json
{
  "branch_code": "NAIROBI-HQ", "tin": "A123456789Z", "bhf_id": "00", "status": "ACTIVE",
  "device_type": "OSCU", "kra_base_url_override": null,
  "dvc_id": "...", "sdc_id": "...", "mrc_no": "..."
}
```

**Errors:**
- `403` — missing/wrong admin key.
- `409` — KRA reports `902` (already installed) but no local branch record exists for this
  `(tin, bhfId)`; escalate to ops manually rather than re-initializing (the existing `cmcKey`
  can't be recovered from KRA). If a local record *does* exist, this is instead treated as
  idempotent and returns it with `201`.
- `422` — `device_type="VSCU"` with no `KRA_BASE_URL_VSCU_{SBX,PROD}` configured and no
  `kra_base_url_override` given.
- `502` — any other KRA error.

---

## Admin — Billing

Admin-only (`X-Admin-Api-Key` or an admin portal bearer token). This is the gateway's own
tenant billing — entirely outside the KRA spec. Internal tracking only: no payment processor
integration. See
[`billing_service.py`](../app/services/billing_service.py) for the enforcement rules and the
[onboarding doc](client_onboarding_and_monitoring.md) for the full lifecycle story.

### `PUT /v1/billing/subscriptions/{client_id}`

Creates the subscription if none exists, else renews `plan_name`/`current_period_end`
/`grace_period_days` on the existing one. **Never touches `status`** — renewing the paid-through
date can't silently lift a `SUSPENDED`/`CANCELLED` state; use the explicit endpoints below for that.

**Request:**

| Field | Type | Notes |
|---|---|---|
| `plan_name` | string | Default `"STANDARD"` — free text, not enum-constrained |
| `current_period_end` | datetime (ISO 8601) | Paid-through date. Naive datetimes are treated as UTC |
| `grace_period_days` | int, optional | Overrides the global `BILLING_GRACE_PERIOD_DAYS` (default 7) for this client only |

**Response** `200`:
```json
{
  "client_id": "...", "plan_name": "STANDARD", "status": "ACTIVE",
  "current_period_end": "2026-12-31T00:00:00Z", "grace_period_days": null,
  "suspended_at": null, "suspended_reason": null
}
```

**Errors:** `404` — no `Client` row with that id.

### `GET /v1/billing/subscriptions/{client_id}`

Returns the same shape as above. `404` if no subscription is on file for this client.

### `POST /v1/billing/subscriptions/{client_id}/suspend`

Blocks the client immediately, regardless of `current_period_end` — e.g. fraud, chargeback.

**Request:** `{"reason": "chargeback on invoice INV-002"}`

**Response** `200`: subscription with `status="SUSPENDED"`, `suspended_at` set,
`suspended_reason` set. **Errors:** `404` — no subscription on file.

### `POST /v1/billing/subscriptions/{client_id}/reactivate`

Clears an explicit suspension (`status` → `ACTIVE`, `suspended_at`/`suspended_reason` cleared).
This is the *only* thing that lifts a suspension — renewing the period does not.
**Errors:** `404` — no subscription on file.

### `POST /v1/billing/subscriptions/{client_id}/cancel`

Terminates the subscription (`status="CANCELLED"`) — permanent, distinct from `SUSPENDED`.
**Errors:** `404` — no subscription on file.

### `POST /v1/billing/clients/{client_id}/invoices`

Records a billing-period invoice. Purely a record — does not by itself change subscription state.

**Request:**

| Field | Type | Notes |
|---|---|---|
| `period_start` | datetime | |
| `period_end` | datetime | |
| `amount` | decimal | |
| `currency` | string | Default `"KES"` |
| `notes` | string, optional | |

**Response** `201`:
```json
{
  "id": "...", "client_id": "...", "period_start": "2026-01-01T00:00:00Z",
  "period_end": "2026-02-01T00:00:00Z", "amount": "5000.00", "currency": "KES",
  "status": "ISSUED", "issued_at": "2026-01-01T00:03:11Z", "paid_at": null, "notes": null
}
```

**Errors:** `404` — no `Client` row with that id.

### `GET /v1/billing/clients/{client_id}/invoices`

Lists that client's invoices, newest first. Returns `[]` if none exist (not a `404`).

### `POST /v1/billing/invoices/{invoice_id}/mark-paid`

Records payment. Optionally renews the subscription's `current_period_end` in the same call.

**Request:** `{"extend_subscription_to": "2026-02-01T00:00:00Z"}` — omit (or `null`) to record
payment without touching the subscription.

**Response** `200`: the invoice with `status="PAID"`, `paid_at` set.
**Errors:** `404` — no `Invoice` row with that id.

---

## Client — Reference Data

### `GET /v1/codes/{classification}`

Cached KRA code tables — never a live KRA call. `classification` is a friendly slug (e.g.
`tax-type`, `payment-method`, `quantity-unit`, `packaging-unit`, `product-type` — see
`CLASSIFICATION_SLUGS` in `app/services/code_cache_service.py`) or the raw KRA `cdCls` number.

**Response** `200`:
```json
[{"cd": "01", "cd_nm": "CASH", "cd_desc": null, "srt_ord": 1, "use_yn": "Y", "refreshed_at": "2026-08-27T06:00:00Z"}]
```

---

## Client — Master Data

### `POST /v1/items`

Registers or updates an item. Posting an existing `sku` again **updates** it and reuses the
previously-assigned `item_cd`, rather than creating a duplicate.

**Request:**

| Field | Type | Notes |
|---|---|---|
| `branch_code` | string | |
| `sku` | string | Your own identifier |
| `name` | string | |
| `item_classification_cd` | string | Raw KRA classification code — no friendly crosswalk exists for this field |
| `unit_of_measure` | string | Friendly code (`GET /v1/codes/quantity-unit`) or raw KRA code |
| `packaging_unit` | string | Friendly code (`GET /v1/codes/packaging-unit`) or raw KRA code |
| `product_type` | `RAW_MATERIAL`\|`FINISHED_PRODUCT`\|`SERVICE` | |
| `tax_category` | `EXEMPT`\|`STANDARD16`\|`ZERO`\|`NONVAT`\|`8PCT` | |
| `unit_price` | decimal | |
| `origin_nation_cd` | string | Default `"KE"` |
| `insurance_applicable` | bool | Default `false` |

**Response** `201`:
```json
{
  "branch_code": "NAIROBI-HQ", "sku": "BREAD-400G", "item_cd": "KE2NTBA0000001",
  "item_cls_cd": "5020150101", "name": "Bread 400g", "tax_category_cd": "B",
  "unit_price": "100.00", "kra_synced_at": "2026-08-27T12:00:00Z"
}
```

**Errors:** `403` (branch not allowed), `422` (unresolvable `unit_of_measure`/`packaging_unit`),
`502` (KRA rejected `saveItem`).

### `GET /v1/items`

`branch_code` query param optional — omit to list everything the client's `allowed_branch_ids`
covers. Response: array of the same shape as above.

### `POST /v1/items/{sku}/composition`

Reports an item's raw-material composition. **No Pydantic schema exists for this payload** —
spec §5.1/§20 never confirms the field list or even the endpoint URL, so `composition` is
passed through to KRA opaquely, merged with `tin`/`bhfId`/`cmcKey`/`itemCd`.

**Request:** `{"branch_code": "...", "composition": {<opaque KRA-shaped fields>}}`

**Response** `202`: `{"resultCd": "000", "resultMsg": "..."}`. **Errors:** `404` (unknown
`sku` for that branch), `502` (KRA error).

### `POST /v1/branches/customers`

Direct 1:1 pass-through to KRA's `saveBhfCustomer`.

**Request:** `branch_code`, `cust_no` (≤9), `cust_tin` (≤11), `cust_nm` (≤60), and optional
`adrs`, `tel_no`, `email`, `fax_no`, `remark`.

**Response** `201`: `{"resultCd": "...", "resultMsg": "..."}`.

### `POST /v1/branches/users`

Pass-through to `saveBhfUser`. **Request:** `branch_code`, `user_id` (≤20), `user_nm` (≤60),
`pwd` (≤255). **Response** `201`: same shape as above.

### `POST /v1/branches/insurance`

Pass-through to `saveBhfInsurance`. **Request:** `branch_code`, `isrcc_cd` (≤10), `isrcc_nm`
(≤100), `isrc_rt` (decimal, ≤3 digits). **Response** `201`: same shape as above.

---

## Client — Sales (synchronous, fiscal path)

### `POST /v1/sales-invoices`

**Synchronous** — the fiscal receipt proof is in the response before the call returns (spec
§14's own recommendation, since it's typically needed immediately to print a compliant
receipt). Every referenced `sku` must already be registered via `POST /v1/items`.

**Request:**

| Field | Type | Notes |
|---|---|---|
| `branch_code` | string | |
| `external_invoice_ref` | string | **Idempotency key** — always resend the same value for retries of the same invoice, or you'll create a duplicate fiscal receipt |
| `customer_pin` | string, optional | |
| `customer_name` | string, optional | |
| `payment_method` | string | Friendly code (`GET /v1/codes/payment-method`) or raw KRA code |
| `invoice_date` | date | |
| `lines` | array | Each: `sku`, `description`, `quantity`, `unit_of_measure`, `unit_price`, `discount_pct` (default `0`), `tax_category` |

**Response** `201`:
```json
{
  "external_ref": "SAP-BILLDOC-0001", "branch_code": "NAIROBI-HQ", "status": "FISCALIZED",
  "kra_cur_rcpt_no": 1, "kra_tot_rcpt_no": 1,
  "kra_rcpt_sign": "2222222222222222", "kra_intrl_data": "11111111111111111111111111",
  "kra_sdc_datetime": "20200226143124"
}
```

Resubmitting the same `external_invoice_ref` returns this same cached body (`201`) without
calling KRA again. A **concurrent** resubmission (still in flight) gets `409` instead.

**Errors:** `409` (idempotency race), `422` (unregistered `sku` or unresolvable code — the
idempotency claim is released so the client can retry), `502` (KRA rejected the invoice — claim
also released).

### `GET /v1/sales-invoices/{external_ref}`

Query param: `branch_code` (required). Returns the same shape as the POST response, or `404`.

---

## Client — Purchases, Stock, Imports (asynchronous, outbox pattern)

These return `202 Accepted` immediately; the actual KRA submission happens in the background
via `app/workers/outbox_dispatcher.py` (retry with backoff: 30s / 2m / 10m / 1h, then `FAILED`).
Poll the relevant `GET` to confirm completion.

### `GET /v1/purchases/pending`

Query param: `branch_code` (required). Lists supplier-declared sales KRA told the gateway
about (`/selectTrnsPurchaseSalesList`), cached locally, awaiting confirmation.

**Response** `200`:
```json
[{"branch_code": "...", "spplr_tin": "P987654321B", "spplr_invc_no": "SUP-INV-0001", "tot_amt": "1200.00", "item_list": [...]}]
```

### `POST /v1/purchases`

Confirms one pending purchase, referencing it by `(spplr_tin, spplr_invc_no)` — amounts/line
items are reconstructed from what was cached, not resupplied by the client.

**Request:** `branch_code`, `external_ref`, `spplr_tin`, `spplr_invc_no`, `pchs_ty_cd` (raw KRA
Transaction Type — spec gives only the bare codes `C`/`N`/`P`/`T`, no crosswalk).

**Response** `202`: `{"branch_code": "...", "external_ref": "...", "status": "PENDING"}`.
**Errors:** `404` — no matching cached purchase for that `(spplr_tin, spplr_invc_no)`.

### `GET /v1/stock/movements`

Query param: `branch_code` (required). Cached `/selectStockMoveList` entries — spec gives no
field list for this endpoint's response, so each row is `raw_data` (a permissive JSONB blob)
plus `synced_at`.

### `POST /v1/stock/movements`

**Request:** `branch_code`, `external_ref`, `sar_ty_cd` (raw KRA code — `01`-`06` incoming,
`11`-`16` outgoing, no per-code meanings given), `lines` (each: `sku`, `quantity`, `unit_price`).

**Response** `202`: `{"branch_code": "...", "external_ref": "...", "status": "PENDING"}`.
**Errors:** `422` — a line's `sku` doesn't resolve.

### `POST /v1/stock/master`

Sets an item's remaining quantity directly (`saveStockMaster`).

**Request:** `branch_code`, `sku`, `remaining_quantity`. **Response** `202`:
`{"status": "queued"}`. **Errors:** `422` — unresolvable `sku`.

### `GET /v1/imports`

Query param: `branch_code` (required). Cached customs import items from
`/selectImportItemList`.

**Response** `200`:
```json
[{"branch_code": "...", "task_cd": "...", "dcl_de": "...", "item_seq": 1, "dcl_no": "...", "hs_cd": "...", "item_nm": "...", "orgn_nat_cd": "KE", "impt_item_stts_cd": "2", "synced_at": "..."}]
```

### `POST /v1/imports/{task_cd}/decision`

Approves/rejects a customs import item.

**Request:** `branch_code`, `item_cls_cd`, `item_cd`, `decision_cd` (`1` Unsent / `2` Waiting /
`3` Approved / `4` Cancelled — spec §12.16).

**Response** `202`: `{"status": "queued"}`. **Errors:** `404` — no matching cached import item
for that `task_cd`.

### `GET /v1/notices`

Query param: `branch_code` (required). Cached KRA notices, read-only.

**Response** `200`:
```json
[{"branch_code": "...", "notice_no": "...", "title": "...", "cont": "...", "dtl_url": null, "regr_nm": null, "reg_dt": null}]
```

---

## Ops — Health & Metrics

Neither of these requires client-bearer or admin auth.

### `GET /v1/health`

Liveness/readiness. Reports the most recent successful poll per resource, across all branches
(a per-branch breakdown would grow unbounded with branch count).

**Response** `200`:
```json
{
  "status": "ok",
  "last_successful_poll": {
    "codes": "2026-08-27T06:00:11Z",
    "notices": "2026-08-27T11:15:02Z",
    "import_items": "2026-08-27T11:45:00Z",
    "purchase_sales": "2026-08-27T11:45:03Z",
    "stock_moves": "2026-08-27T11:45:05Z"
  }
}
```
A resource missing from the map means no poll has ever succeeded for it.

### `GET /metrics`

Prometheus scrape endpoint (`text/plain` exposition format). `outbox_queue_depth` and
`kra_sync_lag_seconds` are DB-backed gauges refreshed at scrape time, so they're never stale
between scrapes. See [`observability.md`](observability.md) for the full metric list and
example PromQL queries.
