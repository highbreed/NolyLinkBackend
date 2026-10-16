# Onboarding and Monitoring a Client — A Walkthrough

This is a narrative walkthrough of the full lifecycle: registering a new client's fiscal
devices, issuing them API credentials, setting up billing, watching them operate, and handling
what happens when a payment lapses. Every step names the exact endpoint used. For the
exhaustive per-endpoint reference, see [`api_reference.md`](api_reference.md).

**Cast:**
- **Amina** — Integrations Lead at the company operating this gateway. She has the
  `ADMIN_API_KEY` and database access. Everything in "Onboarding" below is her doing.
- **Jitihada Retail Ltd** — a new **Customer** going live on the gateway: the paying business,
  which owns branch access and billing (2026-09-01 — see `Customer`'s docstring in
  `app/db/models.py`). They have two branches: **Nairobi HQ** (a standard OSCU device) and
  **Mombasa Port Branch** (whose fiscal setup is VSCU — included here specifically to show that
  path).
- **"JITIHADA-SAP"** — the **Client** connector credential Jitihada's SAP platform
  authenticates with. A Customer can have more than one of these (e.g. a separate connector for
  a staging environment, or two different systems integrating on their behalf) — Jitihada only
  needs the one for now.
- **Brian** — Jitihada's SAP integration developer. Everything in "Going live" is his doing,
  using only [`sap_integration_guide.md`](sap_integration_guide.md) — he never needs to know
  KRA's API at all.

---

## Part 1 — Onboarding (Amina)

Steps 1-2 below walk through device registration and Customer creation as two separate calls,
for clarity. A stepper-form admin UI collecting name + all branch info up front should instead
call `POST /v1/customers/onboard` once (2026-09-01) — it does both in a single request and
returns one response with a per-branch status, rather than Amina making one `/v1/devices/
initialize` call per branch followed by a separate `/v1/customers` call. See
[`api_reference.md`](api_reference.md#post-v1customersonboard) for its full shape, including
why a partial failure (one branch registers, another doesn't) still returns `201` with the
Customer created for whichever branches succeeded, rather than an all-or-nothing rollback.

### Step 1: Register each fiscal device

Before Jitihada can send a single request, each of their branches needs a KRA-registered
device. This is one-time per `(tin, bhfId)` and only Amina can do it
(`POST /v1/devices/initialize`, gated by `X-Admin-Api-Key` — never reachable by Jitihada's own
systems, spec §12.2's reasoning).

```bash
curl -X POST https://gateway.example.com/v1/devices/initialize \
  -H "X-Admin-Api-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"branch_code": "JITIHADA-NAIROBI-HQ", "tin": "P051234567X", "bhf_id": "00"}'
```

Nairobi HQ is a plain OSCU device, so `device_type` is left at its default — this call reaches
real KRA and persists the branch's `cmcKey` encrypted at rest (the response never contains it).
For Mombasa, Jitihada's fiscal setup is VSCU — a genuinely different request, not just a
different host: this call makes **no** KRA call at all (there is no KRA-hosted registration
endpoint for VSCU — see `docs/kra_specs/README.md`'s 2026-09-02 follow-up correction) and just
creates the branch locally, with no `cmcKey` to store:

```bash
curl -X POST https://gateway.example.com/v1/devices/initialize \
  -H "X-Admin-Api-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"branch_code": "JITIHADA-MOMBASA-PORT", "tin": "P051234567X", "bhf_id": "01", "device_type": "VSCU"}'
```

Mombasa isn't actually *live* yet after this call — a VSCU device still needs its own local-app
initialization (`POST /v1/vscu/{branch_id}/initialize`, giving it the device's `base_url` on
Jitihada's network), covered in Step 3's onboarding-endpoint note below.

At this point `branches` has two rows, each with its own `tin`/`bhfId`/`cmcKey` — nothing
from here on ever mixes them up, because every later call addresses a branch by
`branch_code`, never the raw KRA identifiers.

### Step 2: Create the Customer

Amina creates the Customer — the tenant that will own branch access and billing
(`POST /v1/customers`, admin-only; this used to be a manual raw SQL insert, closed 2026-09-01):

```bash
curl -X POST https://gateway.example.com/v1/customers \
  -H "X-Admin-Api-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"name": "Jitihada Retail Ltd", "branch_codes": ["JITIHADA-NAIROBI-HQ", "JITIHADA-MOMBASA-PORT"]}'
```

`branch_codes` is the branch allow-list (spec §15's model, now living on the Customer rather
than the connector) — resolved server-side to the branch ids created in Step 1, by
`branch_code`, never raw UUIDs. The response's `id` is Jitihada's `customer_id`; Amina notes it
for the next two steps.

### Step 3: Create the connector credential

```bash
curl -X POST https://gateway.example.com/v1/customers/<jitihada customer id>/connectors \
  -H "X-Admin-Api-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"name": "JITIHADA-SAP"}'
```

**Response** (shown exactly once — the plaintext secret can't be recovered later, only
re-issued by creating a new connector):
```json
{"client_id": "...", "client_secret": "<raw secret>", "name": "JITIHADA-SAP"}
```

Every connector created this way inherits Jitihada's branch access and billing status
automatically — nothing further to configure per connector. If Jitihada later needs a second
system integrating on their behalf (a staging environment, a different POS vendor), Amina just
calls this endpoint again under the same `customer_id`; she does **not** create a second
Customer for it.

### Step 4: Set up billing

Jitihada has signed a contract for the `STANDARD` plan, paid through the end of the year.
Amina records that against the Customer, not the connector
(`PUT /v1/billing/subscriptions/{customer_id}`, admin-only):

```bash
curl -X PUT https://gateway.example.com/v1/billing/subscriptions/<jitihada customer id> \
  -H "X-Admin-Api-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"plan_name": "STANDARD", "current_period_end": "2026-12-31T00:00:00Z"}'
```

This is internal tracking only — no payment processor is involved. If
`BILLING_ENFORCEMENT_ENABLED=true` on this deployment, every one of Jitihada's connectors would
be rejected with `402` had this step been skipped (no subscription on file = blocked); doing
this *before* handing over credentials means Brian's very first request just works.

### Step 5: Hand off

Amina sends Brian, out-of-band (not through the gateway — this is a one-time secret):
- The `client_id` from Step 3
- The raw `client_secret` from Step 3
- The two `branch_code` values from Step 1: `JITIHADA-NAIROBI-HQ`, `JITIHADA-MOMBASA-PORT`
- A link to [`sap_integration_guide.md`](sap_integration_guide.md)

Onboarding is done. Everything from here is Brian's side, using only client-bearer auth.

---

## Part 2 — Going live (Brian)

### Step 6: Get a token

```bash
curl -X POST https://gateway.example.com/oauth/token \
  -d "grant_type=client_credentials&client_id=<client id>&client_secret=<raw secret>"
```
Brian's SAP integration platform re-requests this whenever a call comes back `401` (tokens
expire after 30 minutes; there's no refresh grant).

### Step 7: Register items

Before the first sale, every SKU Jitihada sells needs registering once
(`POST /v1/items`) — this is what assigns the KRA-format `item_cd`:

```bash
curl -X POST https://gateway.example.com/v1/items \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"branch_code": "JITIHADA-NAIROBI-HQ", "sku": "RICE-2KG", "name": "Rice 2kg",
       "item_classification_cd": "5059690800", "unit_of_measure": "KG",
       "packaging_unit": "BG", "product_type": "FINISHED_PRODUCT",
       "tax_category": "STANDARD16", "unit_price": "350.00"}'
```

### Step 8: Sell things

Every sale at either branch goes through `POST /v1/sales-invoices`, synchronously — Brian gets
the fiscal receipt fields (`kra_rcpt_sign`, `kra_cur_rcpt_no`, ...) back in the same response,
in time to print the till receipt. He always sends the same `external_invoice_ref` for retries
of the same sale (it's the idempotency key — a fresh ref on retry would double-fiscalize).

### Step 9: Purchases, stock, imports run in the background

`GET /v1/purchases/pending`, `POST /v1/purchases`, `POST /v1/stock/movements`,
`GET /v1/imports` + `POST /v1/imports/{taskCd}/decision` — these all return `202` immediately
and settle with KRA asynchronously (the outbox pattern). Brian polls the matching `GET` if his
system needs to confirm completion rather than assume it.

---

## Part 3 — Monitoring

### Amina (ops, ongoing)

Once Jitihada is live, Amina's day-to-day tools are her admin key (now including two purpose-
built read endpoints, 2026-09-01 — no more hand-querying tables for the common cases) or
unauthenticated ops endpoints. Never Jitihada's own credentials.

| Question | Where Amina looks |
|---|---|
| "Is the gateway itself up?" | `GET /v1/health` — `{"status": "ok", ...}` |
| "Are the background pollers keeping up?" | `GET /v1/health`'s `last_successful_poll` map, or `kra_sync_lag_seconds{resource=...}` in `GET /metrics` — a resource missing from either means it's never succeeded |
| "Is the outbox backing up for anyone?" | `outbox_queue_depth{status=~"PENDING\|RETRYING"}` (backlog) and `outbox_queue_depth{status="FAILED"}` (needs manual intervention — see [`observability.md`](observability.md)'s PromQL) |
| "How is KRA responding to calls generally?" | `kra_calls_total` by `result_cd` — a spike in anything other than `000`/`001` is the first place to look |
| "What branches exist, and is Jitihada's VSCU device healthy?" | `GET /v1/admin/branches` (or `/v1/admin/branches/{branch_code}`) — device health (`last_successful_call_at`, `last_error`, last-known sequence numbers) inlined for VSCU branches |
| "What is *Jitihada specifically* doing / how is it going for them?" | `GET /v1/admin/call-logs?branch_code=JITIHADA-MOMBASA-PORT` — the same admin dashboard read endpoint the frontend admin portal uses, filterable by `branch_code`/`protocol`/`result_cd`/`since`. (Or, unfiltered: `kra_call_log`/`vscu_call_log` directly, or the structured JSON audit log filtered by `client_id` — deliberately **not** a Prometheus label, would blow up cardinality with every tenant.) |
| "Is Jitihada current on payment?" | `GET /v1/billing/subscriptions/<jitihada customer id>` — `status`, `current_period_end`, `grace_period_days` |
| "What connectors does Jitihada have, and who is which?" | `GET /v1/customers/<jitihada customer id>/connectors` |
| "Did a specific request from Jitihada fail, and why?" | The response `X-Request-ID` header (from Brian, if he reports an issue) grepped across API logs — `request_id` is on every log line for that request, not just one summary line |

### Brian (self-service, ongoing)

Unlike Amina's tools above, these use Brian's own client bearer token and are scoped to
Jitihada's own branches only — a genuinely new surface (2026-09-01, see
[`api_reference.md`](api_reference.md#client--monitoring)) that didn't exist before; previously
this table's data was visible only to Amina.

| Question | Where Brian looks |
|---|---|
| "Did invoice X actually fiscalize?" | `GET /v1/monitoring/invoices?branch_code=...` — filterable by `status`/`transaction_kind` |
| "Is my integration's error rate normal?" | `GET /v1/monitoring/call-logs?branch_code=...` — his own OSCU/VSCU call history, filterable by `result_cd` |
| "What did today's Z-report look like?" (VSCU branches only) | `GET /v1/monitoring/branches/{branch_code}/z-report/{rpt_de}` — read-only; OSCU day-closing isn't exposed yet (unverified against the spec, see that endpoint's docs) |

---

## Part 4 — The billing lifecycle

### Renewal

Near the end of the paid period, Amina issues an invoice and, once Jitihada pays (by whatever
means — this module doesn't process payment, only records it), marks it paid and renews the
subscription in one call:

```bash
curl -X POST https://gateway.example.com/v1/billing/customers/<jitihada customer id>/invoices \
  -H "X-Admin-Api-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"period_start": "2027-01-01T00:00:00Z", "period_end": "2027-12-31T00:00:00Z", "amount": "120000.00"}'

curl -X POST https://gateway.example.com/v1/billing/invoices/<invoice id>/mark-paid \
  -H "X-Admin-Api-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"extend_subscription_to": "2027-12-31T00:00:00Z"}'
```

### If payment is late

Nothing breaks the moment `current_period_end` passes — Jitihada keeps working for
`grace_period_days` (default 7, set per-subscription in Step 4 if Jitihada's contract needs a
different window). This absorbs ordinary invoice-processing lag so a slow renewal doesn't look
like an outage to Brian. Only once the grace period itself elapses does every request from
Jitihada's token(s) start returning `402`, with a `detail` explaining exactly why
(`"...lapsed on 2027-12-31 and its 7-day grace period has ended"`).

### If it's not just lateness — suspension

For something that shouldn't wait out a grace period (a chargeback, a contract dispute), Amina
can block Jitihada immediately regardless of their paid-through date. Suspending is
customer-wide — it blocks *every* connector Jitihada has, not just one:

```bash
curl -X POST https://gateway.example.com/v1/billing/subscriptions/<jitihada customer id>/suspend \
  -H "X-Admin-Api-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"reason": "chargeback on invoice INV-0042"}'
```

Every Jitihada request now gets `402` with that reason in `detail`. Critically, a stray
renewal (Step 4's `PUT`) does **not** silently lift this — reactivation is always the
deliberate, separate call:

```bash
curl -X POST https://gateway.example.com/v1/billing/subscriptions/<jitihada customer id>/reactivate \
  -H "X-Admin-Api-Key: $ADMIN_API_KEY"
```

---

## Quick reference: who calls what

| Actor | Auth | Endpoints |
|---|---|---|
| Amina (onboarding) | `X-Admin-Api-Key` | `POST /v1/devices/initialize`, `POST /v1/customers`, `POST /v1/customers/{id}/connectors`, `PUT /v1/billing/subscriptions/{customer_id}` |
| Amina (monitoring) | `X-Admin-Api-Key` / none | `GET /v1/health`, `GET /metrics`, `GET /v1/admin/branches`, `GET /v1/admin/call-logs`, `GET /v1/billing/subscriptions/{customer_id}`, `GET /v1/customers/{id}/connectors`, JSON audit logs |
| Amina (billing lifecycle) | `X-Admin-Api-Key` | `POST /v1/billing/customers/{id}/invoices`, `POST /v1/billing/invoices/{id}/mark-paid`, `.../suspend`, `.../reactivate`, `.../cancel` |
| Brian (setup) | Client bearer | `POST /oauth/token`, `POST /v1/items` |
| Brian (day-to-day) | Client bearer | `POST /v1/sales-invoices`, `POST /v1/sales-invoices/credit-notes`, `GET /v1/sales-invoices/{ref}`, `GET/POST /v1/purchases*`, `GET/POST /v1/stock/*`, `GET/POST /v1/imports*`, `GET /v1/notices`, `GET /v1/codes/{classification}` |
| Brian (self-service monitoring) | Client bearer | `GET /v1/monitoring/invoices`, `GET /v1/monitoring/call-logs`, `GET /v1/monitoring/branches/{branch_code}/z-report/{rpt_de}` |

See [`api_reference.md`](api_reference.md) for the full request/response shape of every row above.
