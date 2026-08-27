# Onboarding and Monitoring a Client — A Walkthrough

This is a narrative walkthrough of the full lifecycle: registering a new client's fiscal
devices, issuing them API credentials, setting up billing, watching them operate, and handling
what happens when a payment lapses. Every step names the exact endpoint used. For the
exhaustive per-endpoint reference, see [`api_reference.md`](api_reference.md).

**Cast:**
- **Amina** — Integrations Lead at the company operating this gateway. She has the
  `ADMIN_API_KEY` and database access. Everything in "Onboarding" below is her doing.
- **Jitihada Retail Ltd** — a new client going live on the gateway, integrating from SAP.
  They have two branches: **Nairobi HQ** (a standard OSCU device) and **Mombasa Port Branch**
  (whose fiscal setup is VSCU — included here specifically to show that path).
- **Brian** — Jitihada's SAP integration developer. Everything in "Going live" is his doing,
  using only [`sap_integration_guide.md`](sap_integration_guide.md) — he never needs to know
  KRA's API at all.

---

## Part 1 — Onboarding (Amina)

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

Nairobi HQ is a plain OSCU device, so `device_type` is left at its default. For Mombasa,
Jitihada's fiscal setup is VSCU — same request, same response shape, only the host differs:

```bash
curl -X POST https://gateway.example.com/v1/devices/initialize \
  -H "X-Admin-Api-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"branch_code": "JITIHADA-MOMBASA-PORT", "tin": "P051234567X", "bhf_id": "01", "device_type": "VSCU"}'
```

That second call resolves to `KRA_BASE_URL_VSCU_SBX`/`_PROD` automatically — Amina doesn't
paste a URL unless Jitihada needs a *specific* non-standard host, in which case she'd add
`"kra_base_url_override": "https://..."` instead. Both calls persist the branch's `cmcKey`
encrypted at rest; neither response ever contains it.

At this point `branches` has two rows, each with its own `tin`/`bhfId`/`cmcKey` — nothing
from here on ever mixes them up, because every later call addresses a branch by
`branch_code`, never the raw KRA identifiers.

### Step 2: Create the client credential

This is the one gap in an otherwise all-API onboarding flow: **there is no
`POST /v1/clients` endpoint today.** Amina creates the row directly:

```sql
INSERT INTO clients (id, name, client_secret_hash, allowed_branch_ids)
VALUES (
    gen_random_uuid(),
    'JITIHADA-SAP',
    -- sha256 of a secret Amina generates and hands to Brian out-of-band, e.g.:
    --   python -c "import secrets; print(secrets.token_urlsafe(32))"
    -- then: python -c "import hashlib; print(hashlib.sha256(b'<that secret>').hexdigest())"
    '<sha256 hex digest>',
    ARRAY['<Nairobi HQ branch uuid>', '<Mombasa Port branch uuid>']::uuid[]
);
```

`allowed_branch_ids` is the branch allow-list (spec §15) — Jitihada's token will only ever be
able to act on these two branches, even though the gateway may host many other clients'
branches too. Amina notes the generated `id` (Jitihada's OAuth2 `client_id`) — she'll hand it
and the raw secret to Brian in Step 4.

*(If this becomes a recurring, higher-volume workflow, promoting this to an admin endpoint —
`POST /v1/billing/clients` or similar, mirroring the pattern `billing.py` and `device.py`
already establish — would close this gap. Flagged here rather than built silently, since it
wasn't asked for.)*

### Step 3: Set up billing

Jitihada has signed a contract for the `STANDARD` plan, paid through the end of the year.
Amina records that (`PUT /v1/billing/subscriptions/{client_id}`, admin-only):

```bash
curl -X PUT https://gateway.example.com/v1/billing/subscriptions/<jitihada client id> \
  -H "X-Admin-Api-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"plan_name": "STANDARD", "current_period_end": "2026-12-31T00:00:00Z"}'
```

This is internal tracking only — no payment processor is involved. If
`BILLING_ENFORCEMENT_ENABLED=true` on this deployment, Jitihada's requests would be rejected
with `402` had this step been skipped (no subscription on file = blocked); doing this *before*
handing over credentials means Brian's very first request just works.

### Step 4: Hand off

Amina sends Brian, out-of-band (not through the gateway — this is a one-time secret):
- The `client_id` from Step 2
- The raw `client_secret` from Step 2
- The two `branch_code` values from Step 1: `JITIHADA-NAIROBI-HQ`, `JITIHADA-MOMBASA-PORT`
- A link to [`sap_integration_guide.md`](sap_integration_guide.md)

Onboarding is done. Everything from here is Brian's side, using only client-bearer auth.

---

## Part 2 — Going live (Brian)

### Step 5: Get a token

```bash
curl -X POST https://gateway.example.com/oauth/token \
  -d "grant_type=client_credentials&client_id=<client id>&client_secret=<raw secret>"
```
Brian's SAP integration platform re-requests this whenever a call comes back `401` (tokens
expire after 30 minutes; there's no refresh grant).

### Step 6: Register items

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

### Step 7: Sell things

Every sale at either branch goes through `POST /v1/sales-invoices`, synchronously — Brian gets
the fiscal receipt fields (`kra_rcpt_sign`, `kra_cur_rcpt_no`, ...) back in the same response,
in time to print the till receipt. He always sends the same `external_invoice_ref` for retries
of the same sale (it's the idempotency key — a fresh ref on retry would double-fiscalize).

### Step 8: Purchases, stock, imports run in the background

`GET /v1/purchases/pending`, `POST /v1/purchases`, `POST /v1/stock/movements`,
`GET /v1/imports` + `POST /v1/imports/{taskCd}/decision` — these all return `202` immediately
and settle with KRA asynchronously (the outbox pattern). Brian polls the matching `GET` if his
system needs to confirm completion rather than assume it.

---

## Part 3 — Monitoring (Amina, ongoing)

Once Jitihada is live, Amina's day-to-day tools are all either unauthenticated ops endpoints
or her own admin key — never Jitihada's client credentials.

| Question | Where Amina looks |
|---|---|
| "Is the gateway itself up?" | `GET /v1/health` — `{"status": "ok", ...}` |
| "Are the background pollers keeping up?" | `GET /v1/health`'s `last_successful_poll` map, or `kra_sync_lag_seconds{resource=...}` in `GET /metrics` — a resource missing from either means it's never succeeded |
| "Is the outbox backing up for anyone?" | `outbox_queue_depth{status=~"PENDING\|RETRYING"}` (backlog) and `outbox_queue_depth{status="FAILED"}` (needs manual intervention — see [`observability.md`](observability.md)'s PromQL) |
| "How is KRA responding to calls generally?" | `kra_calls_total` by `result_cd` — a spike in anything other than `000`/`001` is the first place to look |
| "What is *Jitihada specifically* doing / how is it going for them?" | `kra_call_log` table, filtered by `branch_id` for either of Jitihada's two branches — endpoint, `result_cd`, timestamp per call (never the raw payload; it's a hash) — or the structured JSON audit log, filtered by `client_id`. `client_id` is deliberately **not** a Prometheus label (would blow up cardinality with every tenant), so per-client questions go to logs/DB, not `/metrics` |
| "Is Jitihada current on payment?" | `GET /v1/billing/subscriptions/<jitihada client id>` — `status`, `current_period_end`, `grace_period_days` |
| "Did a specific request from Jitihada fail, and why?" | The response `X-Request-ID` header (from Brian, if he reports an issue) grepped across API logs — `request_id` is on every log line for that request, not just one summary line |

Nothing above requires Jitihada's own bearer token — Amina never needs it, and it wouldn't
grant her anything she doesn't already have via the admin key or database access.

---

## Part 4 — The billing lifecycle

### Renewal

Near the end of the paid period, Amina issues an invoice and, once Jitihada pays (by whatever
means — this module doesn't process payment, only records it), marks it paid and renews the
subscription in one call:

```bash
curl -X POST https://gateway.example.com/v1/billing/clients/<jitihada client id>/invoices \
  -H "X-Admin-Api-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"period_start": "2027-01-01T00:00:00Z", "period_end": "2027-12-31T00:00:00Z", "amount": "120000.00"}'

curl -X POST https://gateway.example.com/v1/billing/invoices/<invoice id>/mark-paid \
  -H "X-Admin-Api-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"extend_subscription_to": "2027-12-31T00:00:00Z"}'
```

### If payment is late

Nothing breaks the moment `current_period_end` passes — Jitihada keeps working for
`grace_period_days` (default 7, set per-subscription in Step 3 if Jitihada's contract needs a
different window). This absorbs ordinary invoice-processing lag so a slow renewal doesn't look
like an outage to Brian. Only once the grace period itself elapses does every request from
Jitihada's token start returning `402`, with a `detail` explaining exactly why
(`"...lapsed on 2027-12-31 and its 7-day grace period has ended"`).

### If it's not just lateness — suspension

For something that shouldn't wait out a grace period (a chargeback, a contract dispute), Amina
can block Jitihada immediately regardless of their paid-through date:

```bash
curl -X POST https://gateway.example.com/v1/billing/subscriptions/<jitihada client id>/suspend \
  -H "X-Admin-Api-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"reason": "chargeback on invoice INV-0042"}'
```

Every Jitihada request now gets `402` with that reason in `detail`. Critically, a stray
renewal (Step 3's `PUT`) does **not** silently lift this — reactivation is always the
deliberate, separate call:

```bash
curl -X POST https://gateway.example.com/v1/billing/subscriptions/<jitihada client id>/reactivate \
  -H "X-Admin-Api-Key: $ADMIN_API_KEY"
```

---

## Quick reference: who calls what

| Actor | Auth | Endpoints |
|---|---|---|
| Amina (onboarding) | `X-Admin-Api-Key` | `POST /v1/devices/initialize`, direct SQL insert into `clients`, `PUT /v1/billing/subscriptions/{id}` |
| Amina (monitoring) | `X-Admin-Api-Key` / none | `GET /v1/health`, `GET /metrics`, `GET /v1/billing/subscriptions/{id}`, `kra_call_log` table, JSON audit logs |
| Amina (billing lifecycle) | `X-Admin-Api-Key` | `POST /v1/billing/clients/{id}/invoices`, `POST /v1/billing/invoices/{id}/mark-paid`, `.../suspend`, `.../reactivate`, `.../cancel` |
| Brian (setup) | Client bearer | `POST /oauth/token`, `POST /v1/items` |
| Brian (day-to-day) | Client bearer | `POST /v1/sales-invoices`, `GET /v1/sales-invoices/{ref}`, `GET/POST /v1/purchases*`, `GET/POST /v1/stock/*`, `GET/POST /v1/imports*`, `GET /v1/notices`, `GET /v1/codes/{classification}` |

See [`api_reference.md`](api_reference.md) for the full request/response shape of every row above.
