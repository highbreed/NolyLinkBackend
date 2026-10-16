# SAP Connector — API Contract as User Stories

Audience: the team building the SAP-side connector (PI/PO, CPI, or custom ABAP — see
[`sap_integration_guide.md`](sap_integration_guide.md) for the two supported transport
patterns). This document restates the same contract as user stories with Given/When/Then
acceptance criteria, so it can double as a backlog for connector development and as sign-off
criteria for QA. It does not introduce any new endpoint or behavior — for the exhaustive field
reference see [`api_reference.md`](api_reference.md); for the narrated end-to-end walkthrough
see [`client_onboarding_and_monitoring.md`](client_onboarding_and_monitoring.md).

**Persona used throughout:** *the Connector* — the SAP/ERP-side integration (PI/PO, CPI, or
custom ABAP) acting under one issued `client_id`/`client_secret`, on behalf of one Customer,
scoped to that Customer's `allowed_branch_ids`.

Every story below assumes onboarding (device registration, Customer creation, connector
credential issuance, billing) is already done by the gateway operator's admin team — that flow
is out of scope for the Connector and is covered separately in
[`client_onboarding_and_monitoring.md`](client_onboarding_and_monitoring.md#part-1--onboarding-amina).

---

## Epic 1 — Authentication

### Story 1.1: Obtain an access token

> As the Connector, I want to exchange my `client_id`/`client_secret` for a short-lived bearer
> token, so that I can authenticate every subsequent call without sending my credentials on
> every request.

**Endpoint:** `POST /oauth/token` (unversioned, no auth)

| Field | Type | Notes |
|---|---|---|
| `grant_type` | string | Must be `client_credentials` |
| `client_id` | string (UUID) | Issued once by the gateway operator |
| `client_secret` | string | Issued once, shown only at creation time |

Body is `application/x-www-form-urlencoded`, not JSON.

**Acceptance criteria**
- **Given** a valid `client_id`/`client_secret` pair, **when** I POST them with
  `grant_type=client_credentials`, **then** I receive `200` with
  `{"access_token": "...", "token_type": "bearer", "expires_in": 1800}`.
- **Given** an unknown `client_id` or wrong `client_secret`, **when** I attempt the exchange,
  **then** I receive `401 invalid_client`.
- **Given** a `grant_type` other than `client_credentials`, **then** I receive
  `400 unsupported_grant_type`.
- There is no refresh grant. I re-request a token with the same credentials whenever any
  call returns `401` — tokens expire after `expires_in` seconds (default 1800 / 30 min).

### Story 1.2: Use the token on every business call

> As the Connector, I want to attach my bearer token to every request, so that the gateway can
> authorize and rate-limit me correctly.

**Acceptance criteria**
- **Given** a valid, unexpired token, **when** I send `Authorization: Bearer <token>` on any
  `/v1/*` route other than `/v1/health`, **then** the request proceeds to that endpoint's own
  logic.
- **Given** a missing, malformed, or expired token, **then** every such route returns `401`
  and I must re-run Story 1.1.
- **Given** my organization's subscription is unpaid/suspended/cancelled (billing, checked
  before rate limiting, checked before the endpoint itself), **then** I receive `402` with a
  `detail` explaining why — I contact my account rep, this is never something KRA-side.
- **Given** I exceed `CLIENT_RATE_LIMIT_PER_MINUTE` (default 120/min), **then** I receive
  `429` and must back off.
- **Given** a `branch_code` I pass on any request is not in my Customer's
  `allowed_branch_ids`, **then** I receive `403` regardless of how valid the rest of the
  payload is.

---

## Epic 2 — Reference data

### Story 2.1: Look up a KRA code table before using a friendly field

> As the Connector, I want to fetch the gateway's cached KRA code tables, so that I can
> resolve values like unit-of-measure or payment method without hardcoding KRA's raw codes.

**Endpoint:** `GET /v1/codes/{classification}` (bearer auth)

`classification` is a friendly slug (`tax-type`, `payment-method`, `quantity-unit`,
`packaging-unit`, `product-type`, `credit-note-reason`, ...) or the raw KRA `cdCls` number.

**Acceptance criteria**
- **Given** a known classification, **when** I GET it, **then** I receive `200` with an array
  of `{cd, cd_nm, cd_desc, srt_ord, use_yn, refreshed_at}` — served from cache, never a live
  KRA call, so it's safe to call often.
- I may instead pass any friendly field (`unit_of_measure`, `packaging_unit`,
  `payment_method`, credit-note `reason`) directly as its human-readable name on the
  endpoints below — the gateway resolves it against this same cache. Passing the raw KRA code
  directly also works.
- **Given** a value that resolves to nothing in either form, **then** the endpoint using it
  returns `422`, not a silent fallback.

---

## Epic 3 — Item master data

### Story 3.1: Register (or update) an item before it can be sold

> As the Connector, I want to register every SKU once, so that the gateway can assign it a
> KRA-format `item_cd` before any sales invoice references it.

**Endpoint:** `POST /v1/items` (bearer auth)

| Field | Type | Notes |
|---|---|---|
| `branch_code` | string | Must be in my allow-list |
| `sku` | string | My own identifier — the gateway's idempotency key for this endpoint |
| `name` | string | |
| `item_classification_cd` | string | Raw KRA code — no friendly crosswalk exists |
| `unit_of_measure` | string | Friendly (`GET /v1/codes/quantity-unit`) or raw KRA code |
| `packaging_unit` | string | Friendly (`GET /v1/codes/packaging-unit`) or raw KRA code |
| `product_type` | enum | `RAW_MATERIAL` \| `FINISHED_PRODUCT` \| `SERVICE` |
| `tax_category` | enum | `EXEMPT` \| `STANDARD16` \| `ZERO` \| `NONVAT` \| `8PCT` |
| `unit_price` | decimal | |
| `origin_nation_cd` | string, optional | Default `"KE"` |
| `insurance_applicable` | bool, optional | Default `false` |

**Acceptance criteria**
- **Given** a `sku` I haven't sent before, **when** I POST it, **then** I receive `201` with
  a gateway-assigned `item_cd`, and the item is now sellable.
- **Given** a `sku` I already registered, **when** I POST it again (e.g. a price change),
  **then** the existing item is **updated in place** and its original `item_cd` is reused —
  this never creates a duplicate.
- **Given** `unit_of_measure`/`packaging_unit` that resolves to nothing, **then** I receive
  `422`.
- **Given** a `branch_code` outside my allow-list, **then** I receive `403`.
- **Given** KRA itself rejects the underlying `saveItem` call, **then** I receive `502` with
  KRA's own `resultCd`/`resultMsg` in `detail`.

### Story 3.2: Confirm what's already registered

> As the Connector, I want to list items already registered at a branch, so that I can
> reconcile my own SKU catalogue against the gateway's before a bulk sync.

**Endpoint:** `GET /v1/items?branch_code=...` (bearer auth, `branch_code` optional — omit to
list everything my allow-list covers)

**Acceptance criteria**
- **Given** items exist for the branch, **then** I receive `200` with an array in the same
  shape as the registration response.

### Story 3.3 (optional, spec-uncertain): Report an item's raw-material composition

> As the Connector, I want to report which raw materials make up a finished item, so that KRA
> has the composition on file for a `FINISHED_PRODUCT` item.

**Endpoint:** `POST /v1/items/{sku}/composition` — `{"branch_code": "...", "composition": {...}}`

**Acceptance criteria**
- Because the source spec never confirms this payload's field list, `composition` is passed
  through to KRA **opaquely** — the Connector is responsible for shaping it correctly; the
  gateway does no validation beyond merging in `tin`/`bhfId`/`cmcKey`/`itemCd`.
- **Given** an unknown `sku` for that branch, **then** `404`. **Given** KRA rejects it, `502`.
- Response is `202` with KRA's raw `{resultCd, resultMsg}`, not a gateway-shaped body.

---

## Epic 4 — Sales invoices (synchronous, fiscal path)

### Story 4.1: Fiscalize a sale and get the receipt proof back immediately

> As the Connector, I want to submit a completed sale and receive the fiscal receipt fields in
> the same response, so that my POS/billing flow can print a compliant receipt without a
> second round trip.

**Endpoint:** `POST /v1/sales-invoices` (bearer auth, synchronous)

| Field | Type | Notes |
|---|---|---|
| `branch_code` | string | |
| `external_invoice_ref` | string | **Idempotency key** — e.g. the SAP billing document number |
| `customer_pin` | string, optional | |
| `customer_name` | string, optional | |
| `payment_method` | string | Friendly or raw KRA code |
| `invoice_date` | date | |
| `lines[]` | array | Each: `sku`, `description`, `quantity`, `unit_of_measure`, `unit_price`, `discount_pct` (default `0`), `tax_category` |

Every `sku` referenced must already exist via Story 3.1.

**Acceptance criteria**
- **Given** a first-time `external_invoice_ref` with valid lines, **when** I POST, **then**
  I receive `201` with `status: "FISCALIZED"` and the fiscal proof fields:
  `kra_cur_rcpt_no`, `kra_tot_rcpt_no`, `kra_rcpt_sign`, `kra_intrl_data`,
  `kra_sdc_datetime` — these print on the till receipt.
- **Given** I resend the **same** `external_invoice_ref` after it already succeeded,
  **then** I receive the identical cached `201` body — KRA is **not** called again. I must
  always resend the same ref for a retry of the same invoice; a fresh ref on retry
  double-fiscalizes a real sale.
- **Given** the same `external_invoice_ref` is still in flight from a concurrent request,
  **then** I receive `409` — I retry shortly, I do not treat this as a permanent failure.
- **Given** a line references an unregistered `sku`, or a friendly code doesn't resolve,
  **then** I receive `422` and the idempotency claim on `external_invoice_ref` is released —
  I may correct and resubmit the same ref.
- **Given** KRA rejects the fiscalization itself, **then** I receive `502` (claim also
  released, same retry story as `422`).
- Tax bucketing (KRA's A–E `taxblAmt`/`taxAmt` split) is computed by the gateway from each
  line's `tax_category` — I never construct it myself.

### Story 4.2: Look up a previously submitted invoice

> As the Connector, I want to re-fetch a sale by my own reference, so that I can recover the
> fiscal proof if my original response was lost (e.g. a timeout on my side after the gateway
> actually committed it).

**Endpoint:** `GET /v1/sales-invoices/{external_ref}?branch_code=...&transaction_kind=SALE`
(bearer auth)

**Acceptance criteria**
- **Given** the ref exists for that branch and `transaction_kind`, **then** `200` with the
  same shape as the original `201`.
- **Given** it doesn't exist, **then** `404` — safe to treat as "never submitted" and retry
  the original POST.
- `transaction_kind` defaults to `"SALE"`; sales and credit notes are independent ref
  namespaces, so the same string can exist as both a sale and (separately) a credit note.

### Story 4.3: Issue a credit note against a prior sale

> As the Connector, I want to reverse or partially credit a previously fiscalized sale, so
> that a return or correction is reflected as its own compliant fiscal transaction.

**Endpoint:** `POST /v1/sales-invoices/credit-notes` (bearer auth, synchronous)

Same shape as Story 4.1, plus:

| Field | Type | Notes |
|---|---|---|
| `original_invoice_ref` | string | The original sale's `external_invoice_ref` — must already be `FISCALIZED` on this branch |
| `reason` | string | Friendly (`GET /v1/codes/credit-note-reason`) or raw KRA code |

**Acceptance criteria**
- **Given** `original_invoice_ref` resolves to a `FISCALIZED` sale on the same branch,
  **when** I POST valid lines and a reason, **then** I receive `201` with the same fiscal
  proof fields as a sale, plus `transaction_kind: "CREDIT_NOTE"` and `original_ref`.
- **Given** `original_invoice_ref` doesn't resolve to a fiscalized sale on this branch —
  including when it resolves to another credit note, not a sale — **then** `422`. A credit
  note is a new fiscal transaction, never a mutation of the original; there is no way to
  edit a sale after the fact.
- Idempotency, `409`, and `502` behavior are identical to Story 4.1, keyed off this story's
  own `external_invoice_ref` (distinct from `original_invoice_ref`).

---

## Epic 5 — Purchases, stock, imports (asynchronous, outbox pattern)

All of Epic 5 returns `202 Accepted` immediately; the actual KRA submission happens in a
background worker with retry/backoff (30s / 2m / 10m / 1h, then `FAILED`). The Connector polls
the matching `GET` if it needs confirmation rather than assuming success.

### Story 5.1: See supplier-declared sales awaiting my confirmation

> As the Connector, I want to list purchases KRA already told the gateway about, so that I can
> reconcile them against my own goods-receipt process before confirming.

**Endpoint:** `GET /v1/purchases/pending?branch_code=...` (bearer auth)

**Acceptance criteria**
- **Given** the gateway has cached entries from KRA's `/selectTrnsPurchaseSalesList`, **then**
  `200` with each row's `spplr_tin`, `spplr_invc_no`, `tot_amt`, `item_list`.

### Story 5.2: Confirm one pending purchase

> As the Connector, I want to confirm a specific supplier invoice by its supplier
> TIN/invoice-number pair, so that KRA records my acknowledgment without me having to resend
> amounts KRA already gave us.

**Endpoint:** `POST /v1/purchases`

| Field | Type | Notes |
|---|---|---|
| `branch_code` | string | |
| `external_ref` | string | My own tracking id for this confirmation |
| `spplr_tin` | string | Must match a row from Story 5.1 |
| `spplr_invc_no` | string | Must match a row from Story 5.1 |
| `pchs_ty_cd` | string | Raw KRA Transaction Type (`C`/`N`/`P`/`T`) — no friendly crosswalk exists |

**Acceptance criteria**
- **Given** `(spplr_tin, spplr_invc_no)` matches a cached pending purchase, **then** `202`
  with `{"status": "PENDING"}` and it is submitted to KRA asynchronously.
- **Given** no match, **then** `404` — the line items/amounts are never resupplied by me,
  only referenced.

### Story 5.3: Report a stock movement

> As the Connector, I want to record a stock in/out event, so that the gateway relays it to
> KRA without me needing to know KRA's own stock-adjustment endpoint.

**Endpoint:** `POST /v1/stock/movements` — `branch_code`, `external_ref`, `sar_ty_cd` (raw KRA
code, `01`-`06` incoming / `11`-`16` outgoing), `lines[]` (`sku`, `quantity`, `unit_price`).

**Acceptance criteria**
- **Given** every line's `sku` is already registered, **then** `202` with
  `{"status": "PENDING"}`.
- **Given** any line's `sku` doesn't resolve, **then** `422`.

### Story 5.4: See cached stock movement history

> As the Connector, I want to list previously synced stock movements, so that I can audit what
> the gateway has already relayed.

**Endpoint:** `GET /v1/stock/movements?branch_code=...` — each row is `raw_data` (opaque, spec
gives no field list for `/selectStockMoveList`) plus `synced_at`.

### Story 5.5: Set an item's remaining quantity directly

> As the Connector, I want to push an absolute stock-on-hand figure for an item, so that
> KRA's stock master reflects my system's count after a stocktake, without expressing it as a
> delta.

**Endpoint:** `POST /v1/stock/master` — `branch_code`, `sku`, `remaining_quantity`.

**Acceptance criteria**
- **Given** a registered `sku`, **then** `202` with `{"status": "queued"}`.
- **Given** an unregistered `sku`, **then** `422`.

### Story 5.6: See customs import items KRA has declared

> As the Connector, I want to list customs import line items KRA has already told the gateway
> about, so that I can decide whether to approve or reject each one.

**Endpoint:** `GET /v1/imports?branch_code=...` — cached rows from
`/selectImportItemList`: `task_cd`, `dcl_de`, `item_seq`, `dcl_no`, `hs_cd`, `item_nm`,
`orgn_nat_cd`, `impt_item_stts_cd`, `synced_at`.

### Story 5.7: Approve or reject a customs import item

> As the Connector, I want to record my decision on an import item, so that KRA's import
> register reflects whether I'm accepting it into stock.

**Endpoint:** `POST /v1/imports/{task_cd}/decision` — `branch_code`, `item_cls_cd`,
`item_cd`, `decision_cd` (`1` Unsent / `2` Waiting / `3` Approved / `4` Cancelled).

**Acceptance criteria**
- **Given** `task_cd` matches a cached import item, **then** `202` with
  `{"status": "queued"}`.
- **Given** no match, **then** `404`.

---

## Epic 6 — Branch master data (direct KRA pass-throughs)

These three are 1:1 pass-throughs to KRA — every field maps directly, no gateway-side
crosswalk. All follow `{"branch_code": "...", ...KRA-shaped fields...}` and respond `201` with
`{resultCd, resultMsg}`.

### Story 6.1: Register a branch-level customer record
`POST /v1/branches/customers` — `cust_no` (≤9), `cust_tin` (≤11), `cust_nm` (≤60), optional
`adrs`, `tel_no`, `email`, `fax_no`, `remark`.

### Story 6.2: Register a branch-level POS user
`POST /v1/branches/users` — `user_id` (≤20), `user_nm` (≤60), `pwd` (≤255).

### Story 6.3: Register a branch-level insurance rate
`POST /v1/branches/insurance` — `isrcc_cd` (≤10), `isrcc_nm` (≤100), `isrc_rt` (decimal, ≤3
digits).

> As the Connector, I want to push branch reference data KRA requires (customers, POS users,
> insurance rates), so that later fiscal transactions referencing them are accepted, without
> the gateway reshaping fields I already have in KRA's own format.

---

## Epic 7 — Self-service monitoring

> As the Connector's operator, I want to check on my own submitted data and error history
> using my own bearer token, so that I don't have to ask the gateway operator's ops team every
> time I need to verify something.

All scoped to my Customer's own `allowed_branch_ids` — never another tenant's data.

### Story 7.1: Confirm an invoice actually fiscalized
**Endpoint:** `GET /v1/monitoring/invoices?branch_code=...&transaction_kind=...&status=...&limit=...`
Returns `external_ref`, `status`, `kra_cur_rcpt_no`, `kra_rcpt_sign`, `created_at`, etc.

### Story 7.2: Check my own integration's error rate
**Endpoint:** `GET /v1/monitoring/call-logs?branch_code=...&result_cd=...&limit=...`
Returns my own OSCU/VSCU call history — endpoint, `result_cd`, `result_msg`, timing. Never
raw request payloads.

### Story 7.3: Read a VSCU branch's day-closing (Z) report
**Endpoint:** `GET /v1/monitoring/branches/{branch_code}/z-report/{rpt_de}` (`rpt_de` =
`YYYYMMDD`)

**Acceptance criteria**
- **Given** the branch is VSCU and a report exists for that date, **then** `200` with the
  report as built by the branch's local VSCU device.
- **Given** the branch is OSCU, **then** `400` — this is deliberately not offered for OSCU,
  since that surface hasn't been confirmed against the spec.
- **Given** the VSCU device itself errors (e.g. no report built yet for that date), **then**
  `502`.
- This is read-only — there is no client-facing way to trigger a fresh Z-report build.

---

## Cross-cutting acceptance criteria (apply to every story above)

- **Given** any request with a missing/expired token, **then** `401`, regardless of endpoint.
- **Given** my subscription is blocked, **then** `402` on every client-bearer route, checked
  before rate limiting and before the endpoint's own logic.
- **Given** a `branch_code` outside my allow-list, **then** `403`.
- **Given** I exceed the rate limit, **then** `429`.
- **Given** KRA itself errors on a synchronous call, **then** `502` with KRA's own
  `resultCd`/`resultMsg` surfaced in `detail` — I log this for my own diagnostics, since
  retrying immediately rarely helps.
- Every response includes an `X-Request-ID` header — I log it alongside my own transaction
  reference so a reported issue can be traced gateway-side without me resending the payload.

---

## Traceability: story → endpoint

| Story | Method & path |
|---|---|
| 1.1 | `POST /oauth/token` |
| 2.1 | `GET /v1/codes/{classification}` |
| 3.1 | `POST /v1/items` |
| 3.2 | `GET /v1/items` |
| 3.3 | `POST /v1/items/{sku}/composition` |
| 4.1 | `POST /v1/sales-invoices` |
| 4.2 | `GET /v1/sales-invoices/{external_ref}` |
| 4.3 | `POST /v1/sales-invoices/credit-notes` |
| 5.1 | `GET /v1/purchases/pending` |
| 5.2 | `POST /v1/purchases` |
| 5.3 | `POST /v1/stock/movements` |
| 5.4 | `GET /v1/stock/movements` |
| 5.5 | `POST /v1/stock/master` |
| 5.6 | `GET /v1/imports` |
| 5.7 | `POST /v1/imports/{task_cd}/decision` |
| 6.1 | `POST /v1/branches/customers` |
| 6.2 | `POST /v1/branches/users` |
| 6.3 | `POST /v1/branches/insurance` |
| 7.1 | `GET /v1/monitoring/invoices` |
| 7.2 | `GET /v1/monitoring/call-logs` |
| 7.3 | `GET /v1/monitoring/branches/{branch_code}/z-report/{rpt_de}` |

See [`api_reference.md`](api_reference.md) for full request/response shapes and every error
case, and [`sap_integration_guide.md`](sap_integration_guide.md) for a sequence diagram of the
sales-invoice flow end to end.
