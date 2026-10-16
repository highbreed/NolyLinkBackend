# Reference Data & Customer Code Assignment

How the gateway caches KRA's reference-code lists (units of measure, packaging units, tax
categories, item classifications, country of origin), how an admin browses that data, and how
an admin restricts which of those codes a given Customer's connectors are allowed to submit.

This is a companion to [`api_reference.md`](api_reference.md) (the exhaustive endpoint
reference) — this document is scoped to one workflow end-to-end: **seed → browse → assign →
enforce**.

---

## 1. The five reference-code fields

Every item a client registers (`POST /v1/items`) carries five KRA reference-code fields:

| Field (client-facing name) | KRA field | Example | Cached in |
|---|---|---|---|
| `unit_of_measure` | `qtyUnitCd` | `"BA"` (Bag) | `kra_code_lists`, `cd_cls = "10"` |
| `packaging_unit` | `pkgUnitCd` | `"NT"` (Net) | `kra_code_lists`, `cd_cls = "17"` |
| `tax_category` | `taxTyCd` | `"B"` (16% VAT) | hardcoded in `generic_mapper.py` — spec gives this table verbatim, no cache needed |
| `item_classification_cd` | `itemClsCd` | `"50180000"` (Bread and bakery products) | `kra_item_classes` |
| `origin_nation_cd` | `orgnNatCd` | `"KE"` (Kenya) | `kra_code_lists`, `cd_cls = "05"` |

Four of the five (everything except tax category) come from lists too large to hardcode —
`quantity-unit`/`packaging-unit` have ~43/~52 real values, `nation` has ~250, and
`item-classification` is a ~1000-row UNSPSC-style hierarchy. This gateway's policy, stated in
`app/services/code_cache_service.py` and `app/data/kra_code_seeds/README.md`, is to **never
fabricate these values** — every value in the cache came from a real KRA/VSCU response, not a
guess.

---

## 2. How the cache gets populated

Two cache tables, two sync mechanisms:

- **`kra_code_lists`** (`cd_cls`, `cd`, `cd_nm`, `cd_desc`, `srt_ord`, `use_yn`) — one row shape
  covers every KRA "classification" (tax-type, packaging-unit, quantity-unit, nation,
  payment-method, and ~15 others). Synced via KRA's `/selectCodeList` (OSCU) or the local VSCU
  app's `/code/selectCodes` (VSCU) — same table, same shape, either transport.
  Service: `app/services/code_cache_service.py`.
- **`kra_item_classes`** (`item_cls_cd`, `item_cls_nm`, `item_cls_lvl`, `tax_ty_cd`,
  `mjr_tg_yn`, `use_yn`) — item classification has no `cd_cls` of its own (it isn't one of the
  numbered classifications `/selectCodeList` serves), so it's a separate table, synced via
  `/selectItemClsList` (OSCU) or `/itemClass/selectItemsClass` (VSCU).
  Service: `app/services/item_class_cache_service.py`.

### Automatic sync

A Celery beat schedule (`app/workers/celery_app.py`) refreshes both tables for every
**ACTIVE** branch, incrementally (only rows changed since the last sync, via KRA's `lastReqDt`
watermark):

| Task | Interval | Applies to |
|---|---|---|
| `poller.refresh_code_lists` | `KRA_POLL_INTERVAL_CODES_SECONDS` (default 6h) | OSCU branches |
| `poller.refresh_item_classes` | `KRA_POLL_INTERVAL_ITEM_CLASSES_SECONDS` (default 6h) | OSCU branches |
| `vscu_poller.refresh_codes` | same interval as `refresh_code_lists` | VSCU branches |
| `vscu_poller.refresh_item_classes` | same interval as `refresh_item_classes` | VSCU branches |

### Manual sync (admin-triggered)

Useful right after a branch is first registered, or to force a refresh without waiting for the
next scheduled poll. Admin-gated (`X-Admin-Api-Key` or an admin portal bearer token).

```
POST /v1/vscu/{branch_id}/codes/sync          # VSCU — kra_code_lists
GET  /v1/vscu/{branch_id}/item-classes         # VSCU — also persists into kra_item_classes
                                                #   as a side effect (this call is normally a
                                                #   live-diagnostic passthrough; it upserts the
                                                #   response into the cache either way)
```

OSCU branches sync automatically via the Celery beat schedule above (there's no equivalent
admin-triggered endpoint for OSCU yet — the pollers cover it).

**Prerequisite:** for a VSCU branch, the local VSCU app (`VscuDeviceState.base_url`) must be
reachable and initialized. If it isn't, `/codes/sync` fails with a `502` carrying the VSCU
app's own error — most commonly a transport error (app unreachable) or `resultCd=899` ("An
error regarding Client occurred") if the app's own device record isn't in a state to answer
data requests. Re-run `POST /v1/vscu/{branch_id}/initialize` (see `api_reference.md`) to
resolve the latter — it's safe to call again on an already-initialized device.

---

## 3. Browsing the cache (admin)

New admin-only endpoints (`app/api/v1/endpoints/admin_reference_data.py`), separate from the
client-facing `GET /v1/codes/{classification}` and `GET /v1/item-classes` — those require a
Client's own OAuth2 token, which an admin operator doesn't have. All three below require
`X-Admin-Api-Key` (or an admin portal bearer token), same as every other `/v1/admin/*` route.

One behavioral difference worth knowing: these admin endpoints always show the **full**
cache, unfiltered — that's the point, since an admin needs to see everything to decide what
to assign (§4). The client-facing equivalents, by contrast, are allow-list-aware (added
2026-10-15): a restricted customer's own `GET /v1/codes/{classification}`/`GET
/v1/item-classes` calls come back pre-filtered to its permitted subset, via
`code_allowlist_service.get_allowed_codes`.

### `GET /v1/admin/reference-data/codes`

Index of every classification actually present in the cache, with a row count — the entry
point for "what can I assign codes from".

```bash
curl -H "X-Admin-Api-Key: $ADMIN_API_KEY" \
  http://localhost:8000/v1/admin/reference-data/codes
```

```json
[
  {"classification": "tax-type", "cd_cls": "04", "cd_cls_nm": "Taxation Type", "code_count": 5},
  {"classification": "nation", "cd_cls": "05", "cd_cls_nm": "Country", "code_count": 252},
  {"classification": "packaging-unit", "cd_cls": "17", "cd_cls_nm": "Packing Unit", "code_count": 52},
  {"classification": "quantity-unit", "cd_cls": "10", "cd_cls_nm": "Quantity Unit", "code_count": 43},
  {"classification": "bank", "cd_cls": "36", "cd_cls_nm": "Bank", "code_count": 47}
]
```

`classification` is the friendly slug when `code_cache_service.CLASSIFICATION_SLUGS` knows one
for that `cd_cls`, otherwise the raw `cd_cls` itself — either form works as the
`classification` value everywhere else in this document. `cd_cls_nm` is **KRA's own real name**
for the classification (captured from `cdClsNm` on sync, stored in `KraCodeClassification` —
see §2) — it's present regardless of whether a friendly slug exists, and is `null` only if no
sync has run since this field was added (2026-09-18). Every slug in §6 below that isn't from
spec §7 was named *from* this field, not guessed.

### `GET /v1/admin/reference-data/codes/{classification}`

The actual codes for one classification. `classification` accepts a friendly slug or a raw
`cd_cls`. Optional `q` filters by a case-insensitive substring match against the code or its
name.

```bash
curl -H "X-Admin-Api-Key: $ADMIN_API_KEY" \
  "http://localhost:8000/v1/admin/reference-data/codes/tax-type"
```

```json
[
  {"cd": "A", "cd_nm": "A-Exempt", "cd_desc": "A-Exempt", "srt_ord": 1, "use_yn": "Y"},
  {"cd": "B", "cd_nm": "B-VAT 16% ", "cd_desc": "B-VAT 16% ", "srt_ord": 2, "use_yn": "Y"},
  {"cd": "C", "cd_nm": "C-Zero Rated", "cd_desc": "C-Zero Rated", "srt_ord": 3, "use_yn": "Y"},
  {"cd": "E", "cd_nm": "E-VAT 8%", "cd_desc": "E-VAT 8%", "srt_ord": 4, "use_yn": "N"},
  {"cd": "D", "cd_nm": "D-Non VAT", "cd_desc": "D-Non VAT", "srt_ord": 5, "use_yn": "Y"}
]
```
(sorted by `srt_ord`, not alphabetically by `cd` — KRA's own ordering, not ours)

```bash
curl -H "X-Admin-Api-Key: $ADMIN_API_KEY" \
  "http://localhost:8000/v1/admin/reference-data/codes/packaging-unit?q=bag"
```

### `GET /v1/admin/reference-data/item-classes`

Item classifications — ~1000 rows, structured as a 3-level UNSPSC-style hierarchy (`item_cls_cd`
is an 8-digit code: first 2 digits = segment/level 1, next 2 = family/level 2, remaining 4 =
class/level 3, e.g. `50000000` → `50180000` → `50180001`). Supports three optional filters,
combinable:

| Param | Meaning | Example |
|---|---|---|
| `level` | `"1"` (segment) / `"2"` (family) / `"3"` (class) | `?level=2` |
| `prefix` | `item_cls_cd` starts with this | `?prefix=50` (everything under Food and Beverage Products) |
| `q` | Case-insensitive substring match on `item_cls_nm` | `?q=bread` |

```bash
curl -H "X-Admin-Api-Key: $ADMIN_API_KEY" \
  "http://localhost:8000/v1/admin/reference-data/item-classes?prefix=50&level=2"
```

```json
[
  {"item_cls_cd": "50100000", "item_cls_nm": "Nuts and seeds", "item_cls_lvl": "2", "tax_ty_cd": null, "mjr_tg_yn": null, "use_yn": "Y"},
  {"item_cls_cd": "50180000", "item_cls_nm": "Bread and bakery products", "item_cls_lvl": "2", "tax_ty_cd": null, "mjr_tg_yn": null, "use_yn": "Y"},
  {"item_cls_cd": "50200000", "item_cls_nm": "Beverages", "item_cls_lvl": "2", "tax_ty_cd": null, "mjr_tg_yn": null, "use_yn": "Y"}
]
```

With no params, returns the full ~1000-row list — fine for a frontend to fetch once and filter
client-side, or use the query params to page server-side instead.

---

## 4. Assigning codes to a Customer

Every Client (connector credential) belongs to exactly one Customer. Code restrictions are
scoped to the **Customer**, not the individual Client — every connector under a Customer
inherits the same allow-list, the same way branch access (`allowed_branch_ids`) already works.

**Default: unrestricted.** A Customer with no allow-list rows for a classification can submit
*any* cached code for that classification — this is the state every Customer is in until an
admin explicitly configures a restriction, so seeding the reference data above never changes
existing behavior on its own.

Model: `CustomerCodeAllowlist` (`app/db/models.py`) — rows of
`(customer_id, classification, code)`. `classification` is the same slug/raw-`cd_cls` value
used above, plus the pseudo-slug `"item-classification"` for item classes (which have no
`cd_cls`). Endpoints live in `app/api/v1/endpoints/customers.py`, admin-gated like the rest of
`/v1/customers/*`.

### `GET /v1/customers/{customer_id}/code-allowlist`

Every classification this Customer currently has a restriction on. A classification with no
entry here is unrestricted for that customer, not "no codes allowed".

```bash
curl -H "X-Admin-Api-Key: $ADMIN_API_KEY" \
  http://localhost:8000/v1/customers/$CUSTOMER_ID/code-allowlist
```

```json
[
  {"classification": "tax-type", "codes": ["A", "B"]},
  {"classification": "packaging-unit", "codes": ["NT", "BG"]}
]
```

Empty list `[]` means this Customer is fully unrestricted across every classification.

### `PUT /v1/customers/{customer_id}/code-allowlist/{classification}`

Replaces the **entire** allowed set for this `(customer, classification)` pair — not a merge.
Passing an empty `codes` list clears the restriction (back to unrestricted for that
classification).

```bash
curl -X PUT -H "X-Admin-Api-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"codes": ["B"]}' \
  http://localhost:8000/v1/customers/$CUSTOMER_ID/code-allowlist/tax-type
```

```json
{"classification": "tax-type", "codes": ["B"]}
```

**Errors:** `404` — unknown `customer_id`.

### End-to-end example: restrict a customer to VAT-16%-only, "Net" packaging only

```bash
# 1. Browse what's available (optional — confirms the exact code values to use)
curl -H "X-Admin-Api-Key: $ADMIN_API_KEY" \
  "http://localhost:8000/v1/admin/reference-data/codes/tax-type"
curl -H "X-Admin-Api-Key: $ADMIN_API_KEY" \
  "http://localhost:8000/v1/admin/reference-data/codes/packaging-unit?q=net"

# 2. Assign
curl -X PUT -H "X-Admin-Api-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"codes": ["B"]}' \
  http://localhost:8000/v1/customers/$CUSTOMER_ID/code-allowlist/tax-type
curl -X PUT -H "X-Admin-Api-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"codes": ["NT"]}' \
  http://localhost:8000/v1/customers/$CUSTOMER_ID/code-allowlist/packaging-unit

# 3. Verify
curl -H "X-Admin-Api-Key: $ADMIN_API_KEY" \
  http://localhost:8000/v1/customers/$CUSTOMER_ID/code-allowlist
```

---

## 5. How enforcement actually works

Checked in `app/services/mapping/generic_mapper.py` via
`app/services/code_allowlist_service.py`'s `enforce_customer_code_allowlist` — a no-op unless
the customer has configured rows for that classification, otherwise raises the same
`CodeLookupError` an unmapped friendly code already raises (`422`, same error shape).

- **At item registration** (`POST /v1/items`): all five fields are checked —
  `unit_of_measure`, `packaging_unit`, `tax_category`, `item_classification_cd`,
  `origin_nation_cd`.
- **At sales/credit-note submission** (`POST /v1/sales-invoices`,
  `POST /v1/sales-invoices/credit-notes`): only `tax_category` is re-checked, per line. The
  other four aren't re-submitted at sale time (they're read back from the item registered
  earlier, already checked then) — but `tax_category` *is* independently client-supplied per
  sales line, so it's checked again there too, closing what would otherwise be a bypass.

A rejected code returns `422` with a message like:

```json
{"detail": "Customer is not permitted to use 'tax-type' code 'C'"}
```

---

## 6. Reference: known classifications

From `code_cache_service.CLASSIFICATION_SLUGS`. Any `cd_cls` KRA returns that isn't in this
table is still fully usable (browse/assign it by its raw number) — its real name just shows up
via `cd_cls_nm` on `GET /v1/admin/reference-data/codes` (§3) instead of a slug.

**From spec §7** (KRA's Tax Type/Payment Method/etc. appendix, as documented in the source
integration spec):

| Slug | `cd_cls` | KRA's name (`cd_cls_nm`) | Slug | `cd_cls` | KRA's name (`cd_cls_nm`) |
|---|---|---|---|---|---|
| `tax-type` | `04` | Taxation Type | `transaction-progress` | `11` | Sale Status |
| `taxpayer-status` | `15` | — | `registration-type` | `31` | — |
| `product-type` | `24` | Item Type | `purchase-receipt-type` | `38` | Purchase Receipt Type |
| `nation` | `05` | Country | `stock-in-out` | `12` | Stock I/O Type |
| `packaging-unit` | `17` | Packing Unit | `import-item-status` | `26` | Import Item Status |
| `quantity-unit` | `10` | Quantity Unit | `credit-note-reason` | `32` | Refund Reason |
| `currency` | `33` | Currency | `sales-receipt-type` | `37` | Sales Receipt Type |
| `transaction-type` | `14` | Transaction Type | `payment-method` | `07` | Payment Type |

**Not in spec §7 — named from KRA's own `cdClsNm` on a real sync** (added 2026-09-18, once the
cache actually had live data to inspect; see §2's note on `KraCodeClassification`):

| Slug | `cd_cls` | KRA's name (`cd_cls_nm`) |
|---|---|---|
| `branch-status` | `09` | Branch Status |
| `purchase-status` | `34` | Purchase Status — the identical 6-value set as `transaction-progress` (`11`, "Sale Status"), just KRA's separate number for the purchase side |
| `inventory-adjustment-reason` | `35` | Reason of Inventory Adjustment |
| `bank` | `36` | Bank |
| `tax-office` | `45` | Tax Office |
| `locale` | `48` | LOCALE |
| `category-level` | `49` | Category Level — names the item-classification hierarchy tiers themselves (Segment/Family/Class/Commodity — see §3's item-classes note; only Segment/Family/Class are populated in the currently-seeded data) |

Plus the pseudo-slug `item-classification`, used only for `kra_item_classes` (no real `cd_cls`
— see §1).

---

## 7. Files touched by this feature

| File | Role |
|---|---|
| `app/db/models.py` | `KraCodeList`, `KraItemClass`, `CustomerCodeAllowlist` |
| `app/services/code_cache_service.py` | Sync + query for `kra_code_lists`, classification summary |
| `app/services/item_class_cache_service.py` | Sync + query for `kra_item_classes` |
| `app/services/code_allowlist_service.py` | Allow-list enforcement (`enforce_customer_code_allowlist`) |
| `app/services/mapping/generic_mapper.py` | Wires validation + enforcement into item/sales mapping |
| `app/api/v1/endpoints/admin_reference_data.py` | Admin browsing endpoints (§3) |
| `app/api/v1/endpoints/customers.py` | Admin code-allowlist endpoints (§4) |
| `app/api/v1/endpoints/codes.py`, `item_classes.py` | Client-facing equivalents (need a Client's own OAuth2 token) |
| `app/workers/poller.py`, `vscu_poller.py`, `celery_app.py` | Scheduled sync |
| `alembic/versions/d1f4a6b2c9e0_*.py` | Migration for `kra_item_classes` / `customer_code_allowlist` |
