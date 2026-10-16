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
| **Client bearer (JWT)** | `Authorization: Bearer <token>` | Every `/v1/*` endpoint except `/v1/health` and `/v1/devices/*` — includes `/v1/monitoring/*` (a Customer's own connector can view its own activity, see [Monitoring](#client--monitoring)) |
| **Admin key** | `X-Admin-Api-Key: <key>` | `/v1/devices/initialize`, all of `/v1/customers/*`, `/v1/admin/*`, `/v1/billing/*`, all of `/admin/*` except `/admin/login` itself |
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
| `client_id` | string (UUID) | Issued by `POST /v1/customers/{customer_id}/connectors` — see [Customer Onboarding](#admin--customer-onboarding) |
| `client_secret` | string | The raw secret returned once by that same call (only its sha256 hash is stored server-side) |

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
| `device_type` | `"OSCU"` \| `"VSCU"` | Default `"OSCU"`. Genuinely different behavior, not just a different host (see `app/integrations/vscu/` vs `app/schemas/kra/`; `docs/kra_specs/README.md`, corrected 2026-09-01, follow-up fix 2026-09-02). `"OSCU"` calls real KRA's `/selectInitOsdcInfo` and persists the returned `cmcKey`. `"VSCU"` makes **no** KRA call at all — decompiling KRA's VSCU jar found no KRA-hosted registration endpoint for it — the branch is created locally with no `cmcKey` (the local VSCU app owns its own KRA trust anchor internally); see `POST /v1/vscu/{branch_id}/initialize` for the separate call that actually reaches the customer's local VSCU app. |
| `kra_base_url_override` | string, optional | OSCU-only — pins this branch to a specific host regardless of `device_type` or later changes to the global settings (e.g. a tenant with its own dedicated KRA gateway). Ignored for a VSCU branch, which never calls KRA here at all. |

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

## Admin — Customer Onboarding

Admin-only. Creates a **Customer** (the paying business — owns branch access and billing) and
the **Client** connector credential(s) that integrate on its behalf. A Customer can have
several Clients (2026-09-01 — see `Customer`/`Client` docstrings in `app/db/models.py`): e.g.
one connector for a prod SAP instance and a separate one for a staging environment, or two
different systems both submitting on the same Customer's behalf. Branch access
(`allowed_branch_ids`) and billing (`Subscription`/`Invoice`, see
[Billing](#admin--billing) below) both belong to the Customer, not to any one Client — every
connector under a Customer inherits the same branches and the same billing status.

Replaces what used to be a manual raw SQL insert — see the
[onboarding doc](client_onboarding_and_monitoring.md) for the full walkthrough.

### `POST /v1/customers`

**Request:** `{"name": "Jitihada Retail Ltd", "branch_codes": ["JITIHADA-NAIROBI-HQ", "JITIHADA-MOMBASA-PORT"]}`
— `branch_codes` names branches the same way every other endpoint does (never raw UUIDs); omit
or pass `[]` to create a Customer with no branch access yet and grant it later via `PATCH`.

**Response** `201`:
```json
{"id": "...", "name": "Jitihada Retail Ltd", "branch_codes": ["JITIHADA-MOMBASA-PORT", "JITIHADA-NAIROBI-HQ"], "created_at": "..."}
```
**Errors:** `422` — one or more `branch_codes` don't exist.

### `POST /v1/customers/onboard`

The combined flow for a single-screen/stepper-form onboarding UI (2026-09-01): registers every
branch a new Customer needs — real KRA device registration for OSCU branches, purely local
registration plus a local VSCU app initialization call for VSCU ones (2026-09-02 follow-up fix:
VSCU has no KRA-hosted registration call at all, see `docs/kra_specs/README.md`) — and creates
the Customer, all in one call. Use `POST /v1/customers` above instead when the branches already
exist (already registered via `POST /v1/devices/initialize`) and you just need to link a new
Customer to existing `branch_codes`.

**Request:**
```json
{
  "name": "Jitihada Retail Ltd",
  "branches": [
    {"branch_code": "JITIHADA-NAIROBI-HQ", "tin": "P051234567X", "bhf_id": "00"},
    {
      "branch_code": "JITIHADA-MOMBASA-PORT", "tin": "P051234567X", "bhf_id": "01",
      "device_type": "VSCU", "base_url": "http://192.168.1.10:8088"
    }
  ]
}
```
Per branch: `branch_code`, `tin` (≤11), `bhf_id` (≤2), `device_type` (`"OSCU"`|`"VSCU"`, default
`"OSCU"`), `base_url` (VSCU-only, **required** when `device_type="VSCU"` — the customer's local
VSCU app URL on their own network), `kra_base_url_override` (optional, pins this branch to a
specific KRA host). **Not included:** VSCU's `dvcSrlNo` — KRA issues that once, domain-wide, to
this integration (not per-branch/per-customer), so it's resolved automatically from
`VSCU_DVC_SRL_NO_SBX`/`_PROD` (see `.env.example`), never asked for here.

**Response** `201` — always, even if every branch failed (see below):
```json
{
  "customer": {"id": "...", "name": "Jitihada Retail Ltd", "branch_codes": ["JITIHADA-NAIROBI-HQ"], "created_at": "..."},
  "branches": [
    {"branch_code": "JITIHADA-NAIROBI-HQ", "device_type": "OSCU", "status": "REGISTERED", "branch_id": "...", "error": null},
    {"branch_code": "JITIHADA-MOMBASA-PORT", "device_type": "VSCU", "status": "FAILED", "branch_id": null, "error": "VSCU error ...: connection refused"}
  ]
}
```

**Best-effort, not all-or-nothing** — this is deliberate, not a shortcut: an OSCU branch's KRA
device registration has no "undo" (no delete API), so a real rollback across branches would be
actively dangerous — a branch that registered successfully before a later one failed would
leave KRA thinking it's installed with no local record (the same
`DeviceAlreadyProvisionedButUnknown` trap `POST /v1/devices/initialize` already escalates to
ops for a single branch). So every branch is attempted and committed independently; the
Customer is created afterward with whichever branches succeeded, even zero. Fix the failed
branches and POST the same payload again — safe to retry either way: KRA's own `902` ("already
installed") handles it for OSCU, and re-registering the same `(tin, bhfId)` locally is a
no-op for VSCU (see `docs/kra_specs/README.md`).

A branch also fails (before ever calling KRA) if its `(tin, bhfId)` already belongs to a
*different* customer — guards against a data-entry mistake quietly handing one customer access
to another's branch.

**Errors:** `422` — a VSCU branch is missing `base_url` (request-shape validation, before
anything is attempted).

### `GET /v1/customers`

Lists every customer, newest first.

### `GET /v1/customers/{customer_id}`

Returns the same shape as create. **Errors:** `404`.

### `PATCH /v1/customers/{customer_id}`

**Request:** `{"name": "...", "branch_codes": [...]}` — both optional; only fields present are
updated. `branch_codes` **replaces** the branch list wholesale, it doesn't merge.
**Errors:** `404`; `422` — unknown branch code.

### `POST /v1/customers/{customer_id}/connectors`

Issues a new OAuth2 Client credential under this Customer.

**Request:** `{"name": "JITIHADA-SAP"}`

**Response** `201`:
```json
{"client_id": "...", "client_secret": "<raw secret, shown exactly once>", "name": "JITIHADA-SAP"}
```
`client_secret` is plaintext and **cannot be recovered later** — only its hash is stored. Hand
it and `client_id` to the integrator now (Step 4 of the onboarding walkthrough). If the secret
needs replacing later, use `POST .../rotate` below rather than creating a new connector.
**Errors:** `404` — no such customer.

### `GET /v1/customers/{customer_id}/connectors`

Lists that customer's connectors (`client_id`, `name`, `created_at`, `revoked_at` — never the
secret or its hash). `revoked_at` is `null` for an active connector. **Errors:** `404`.

### `POST /v1/customers/{customer_id}/connectors/{client_id}/revoke`

Soft-revokes the connector (2026-10-15): `Client.revoked_at` is set, so its existing OAuth2
tokens are rejected immediately (`401`) and `POST /oauth/token` refuses to issue it a new one
— but the row, and any fiscal/idempotency history behind it, are never deleted (`clients.id` is
a real foreign key from `idempotency_keys`, among other reasons not to hard-delete it).
Idempotent — revoking an already-revoked connector just returns its current state, not an
error.

**Response** `200`: same shape as `GET .../connectors`' list entries, with `revoked_at` now
set. **Errors:** `404` — no such connector under this customer.

### `POST /v1/customers/{customer_id}/connectors/{client_id}/rotate`

Issues a fresh `client_secret` for the **same** `client_id` — the integrator updates only the
secret it holds, nothing else. A `POST /oauth/token` using the old secret starts failing
(`401`) the moment this runs; any token already issued before rotation stays valid until it
naturally expires (rotation doesn't revoke outstanding tokens, only future issuance). Works
independently of `revoked_at` — rotating a revoked connector's secret doesn't un-revoke it.

**Response** `200`: same shape as `POST .../connectors`'s create response (`client_id`,
`client_secret`, `name`) — the new secret, shown exactly once.
**Errors:** `404` — no such connector under this customer.

---

## Admin — Dashboard

Admin-only. Read-only views over data every other endpoint already writes — for the admin
portal's branch/device-health and call-log screens. No new business logic; see
`app/api/v1/endpoints/admin_dashboard.py`.

### `GET /v1/admin/branches`

Every branch, newest first, with VSCU device health inlined where applicable.

**Response** `200`:
```json
[{
  "id": "...", "branch_code": "JITIHADA-MOMBASA-PORT", "tin": "P051234567X", "bhf_id": "01",
  "device_type": "VSCU", "status": "ACTIVE", "created_at": "...",
  "vscu": {
    "base_url": "http://192.168.1.10:8088", "initialized_at": "...",
    "last_sequence_sync_at": "...", "last_known_invc_no": 42, "last_known_sale_rcpt_no": 42,
    "last_successful_call_at": "...", "last_error": null
  }
}]
```
`vscu` is `null` for an OSCU branch (no device-state row exists for those).

### `GET /v1/admin/branches/{branch_code}`

Same shape as one element above. **Errors:** `404`.

### `GET /v1/admin/call-logs`

Merged, newest-first view across `kra_call_log` (OSCU) and `vscu_call_log` (VSCU). Never
returns raw request payloads — only what those tables already store (endpoint, result code,
timing).

**Query params:** `branch_code` (optional filter), `protocol` (`OSCU`|`VSCU`, optional),
`result_cd` (optional), `since` (ISO datetime, optional), `limit` (default 50, max 200).

**Response** `200`:
```json
[{
  "protocol": "VSCU", "id": 1042, "branch_code": "JITIHADA-MOMBASA-PORT",
  "endpoint": "/trnsSales/saveSales", "result_cd": "000", "result_msg": "...",
  "result_dt": "20260901120000", "http_status": 200, "latency_ms": 340,
  "retry_count": null, "created_at": "..."
}]
```
`http_status`/`latency_ms` are VSCU-only; `retry_count` is OSCU-only — the other is `null`.
Note: `limit` returns the most recent N across both tables combined; there's no cursor-based
offset yet for paging deep into history (see the module docstring if that becomes a real need).

---

## Admin — Billing

Admin-only (`X-Admin-Api-Key` or an admin portal bearer token). This is the gateway's own
tenant billing — entirely outside the KRA spec. Internal tracking only: no payment processor
integration. Keyed by `customer_id` (2026-09-01 — billing belongs to the Customer, not to any
one of its Client connectors, see [Customer Onboarding](#admin--customer-onboarding) above).
See [`billing_service.py`](../app/services/billing_service.py) for the enforcement rules and
the [onboarding doc](client_onboarding_and_monitoring.md) for the full lifecycle story.

### `PUT /v1/billing/subscriptions/{customer_id}`

Creates the subscription if none exists, else renews `plan_name`/`current_period_end`
/`grace_period_days` on the existing one. **Never touches `status`** — renewing the paid-through
date can't silently lift a `SUSPENDED`/`CANCELLED` state; use the explicit endpoints below for that.

**Request:**

| Field | Type | Notes |
|---|---|---|
| `plan_name` | string | Default `"STANDARD"` — free text, not enum-constrained |
| `current_period_end` | datetime (ISO 8601) | Paid-through date. Naive datetimes are treated as UTC |
| `grace_period_days` | int, optional | Overrides the global `BILLING_GRACE_PERIOD_DAYS` (default 7) for this customer only |

**Response** `200`:
```json
{
  "customer_id": "...", "plan_name": "STANDARD", "status": "ACTIVE",
  "current_period_end": "2026-12-31T00:00:00Z", "grace_period_days": null,
  "suspended_at": null, "suspended_reason": null
}
```

**Errors:** `404` — no `Customer` row with that id.

### `GET /v1/billing/subscriptions/{customer_id}`

Returns the same shape as above. `404` if no subscription is on file for this customer.

### `POST /v1/billing/subscriptions/{customer_id}/suspend`

Blocks every connector under this customer immediately, regardless of `current_period_end` —
e.g. fraud, chargeback.

**Request:** `{"reason": "chargeback on invoice INV-002"}`

**Response** `200`: subscription with `status="SUSPENDED"`, `suspended_at` set,
`suspended_reason` set. **Errors:** `404` — no subscription on file.

### `POST /v1/billing/subscriptions/{customer_id}/reactivate`

Clears an explicit suspension (`status` → `ACTIVE`, `suspended_at`/`suspended_reason` cleared).
This is the *only* thing that lifts a suspension — renewing the period does not.
**Errors:** `404` — no subscription on file.

### `POST /v1/billing/subscriptions/{customer_id}/cancel`

Terminates the subscription (`status="CANCELLED"`) — permanent, distinct from `SUSPENDED`.
**Errors:** `404` — no subscription on file.

### `POST /v1/billing/customers/{customer_id}/invoices`

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
  "id": "...", "customer_id": "...", "period_start": "2026-01-01T00:00:00Z",
  "period_end": "2026-02-01T00:00:00Z", "amount": "5000.00", "currency": "KES",
  "status": "ISSUED", "issued_at": "2026-01-01T00:03:11Z", "paid_at": null, "notes": null
}
```

**Errors:** `404` — no `Customer` row with that id.

### `GET /v1/billing/customers/{customer_id}/invoices`

Lists that customer's invoices, newest first. Returns `[]` if none exist (not a `404`).

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

Query params: `branch_code` (required), `transaction_kind` (`"SALE"`|`"CREDIT_NOTE"`, default
`"SALE"` — the two are independent idempotency/ref namespaces). Returns the same shape as the
POST response, or `404`.

### `POST /v1/sales-invoices/credit-notes`

A credit note is its own fiscal transaction referencing an original sale — never a mutation of
it. Same request/response/idempotency shape as `POST /v1/sales-invoices` above, plus:

| Field | Type | Notes |
|---|---|---|
| `original_invoice_ref` | string | The original sale's own `external_invoice_ref` — must already be `FISCALIZED` on this branch |
| `reason` | string | Friendly code (`GET /v1/codes/credit-note-reason`) or raw KRA code |

**Response** `201`: same shape as a sale, plus `"transaction_kind": "CREDIT_NOTE"` and
`"original_ref": "<the original's external_invoice_ref>"`.

**Errors:** as above, plus `422` — `original_invoice_ref` doesn't resolve to a `FISCALIZED`
sale on this branch (including if it resolves to another credit note, not a sale).

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

## Client — Monitoring

Client bearer, not the admin key — a Customer's own connector can view its own activity, always
scoped to `Client.customer.allowed_branch_ids` the same way every other client route is (spec
§15's branch allow-listing). This is genuinely new (2026-09-01): previously the only way to see
what happened for a client was ops looking at `kra_call_log`/`vscu_call_log` directly. See
`app/api/v1/endpoints/monitoring.py`.

### `GET /v1/monitoring/invoices`

**Query params:** `branch_code` (required), `transaction_kind` (`"SALE"`|`"CREDIT_NOTE"`,
optional), `status` (optional, e.g. `FISCALIZED`), `limit` (default 50, max 200).

**Response** `200`:
```json
[{
  "external_ref": "SAP-BILLDOC-0001", "branch_code": "NAIROBI-HQ", "transaction_kind": "SALE",
  "original_ref": null, "status": "FISCALIZED", "kra_cur_rcpt_no": 1,
  "kra_rcpt_sign": "2222222222222222", "created_at": "..."
}]
```

### `GET /v1/monitoring/call-logs`

**Query params:** `branch_code` (required), `result_cd` (optional), `limit` (default 50, max
200). Reads `kra_call_log` for an OSCU branch, `vscu_call_log` for a VSCU one — never a raw
request payload, only what those tables already store.

**Response** `200`:
```json
[{
  "protocol": "OSCU", "endpoint": "/saveTrnsSalesOsdc", "result_cd": "000",
  "result_msg": "It is succeeded", "result_dt": "20260901120000",
  "http_status": null, "latency_ms": null, "created_at": "..."
}]
```

### `GET /v1/monitoring/branches/{branch_code}/z-report/{rpt_de}`

Reads a VSCU day-closing (Z) report already built by the branch's local VSCU device —
`rpt_de` is the report date, `YYYYMMDD`. Read-only: there's no client-facing way to *trigger* a
fresh Z-report build (that's an admin/diagnostic action — `POST` on the equivalent admin route
under `/v1/vscu/{branch_id}/reports/z/save`).

**Errors:** `400` — the branch is OSCU, not VSCU. Whether OSCU has an equivalent day-closing
report has not been verified against the spec/decompiled jar the way VSCU's was, so this
deliberately refuses rather than fabricating a contract that hasn't been confirmed to exist.
`502` — the VSCU device itself returned an error (e.g. no report exists yet for that date).

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
