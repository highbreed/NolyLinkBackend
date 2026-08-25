# Technical Specification
## FastAPI OSCU Integration Gateway for KRA eTIMS
### (Middleware for SAP / ERP / POS Client Integration)

**Version:** 1.0
**Source reference:** KRA "Online Sales Control Unit (OSCU) Requirements & Communication Protocols" v2.0 (April 2023)
**Author:** Technical Specification derived for internal engineering use

---

## 1. Purpose & Scope

KRA's OSCU is a JSON/REST API that a taxpayer's "Trader Invoicing System" (TIS) must talk to directly, or via KRA's Java OSCU library. Since our source systems (SAP, other ERPs, POS terminals) cannot easily embed a Java library or talk to KRA's protocol directly, we will build a **FastAPI-based integration gateway** that:

1. Exposes simple, stable, internally-documented REST endpoints that client systems (SAP IDocs/OData push, custom ERPs, POS systems) call to submit business events (item master, sales invoices, purchases, stock movements, branch/customer master data, etc.).
2. Translates and forwards these events to KRA's eTIMS OSCU API using the exact JSON contracts defined in the KRA spec.
3. Persists all KRA master/reference data (codes, item classifications, notices) and transaction results (invoice numbers, receipt signatures, control-unit datetime) so client systems don't need to know KRA's protocol at all.
4. Handles KRA-specific concerns: device initialization & `cmcKey` (communication key) lifecycle, incremental polling (`lastReqDt`), retries, and KRA response-code handling — all abstracted away from client systems.

Out of scope: building the actual Java-OSCU library replacement cryptography (KRA's SKMM signs receipts server-side and returns `rcptSign`/`intrlData`; our job is transport + orchestration, not signature generation).

---

## 2. High-Level Architecture

```
 ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
 │   SAP (IDoc/  │   │  Other ERP   │   │  POS / Web   │
 │  OData/BAPI)  │   │  (REST push) │   │   Clients     │
 └──────┬───────┘   └──────┬───────┘   └──────┬───────┘
        │  HTTPS + API Key/OAuth2 (mTLS optional)
        ▼──────────────────▼──────────────────▼
 ┌─────────────────────────────────────────────────┐
 │        FastAPI OSCU Integration Gateway          │
 │  ┌───────────┐ ┌────────────┐ ┌───────────────┐  │
 │  │ Ingestion │ │ Validation │ │ KRA Adapter   │  │
 │  │ Endpoints │→│ (Pydantic) │→│ (httpx client)│──┼──► KRA eTIMS OSCU API
 │  └───────────┘ └────────────┘ └───────────────┘  │      (Prod / Sandbox)
 │  ┌───────────┐ ┌────────────┐ ┌───────────────┐  │
 │  │ Outbox /  │ │  Scheduler │ │  Postgres DB  │  │
 │  │ Retry Q   │ │ (polling)  │ │  (state store)│  │
 │  └───────────┘ └────────────┘ └───────────────┘  │
 └─────────────────────────────────────────────────┘
```

**Key design principle:** client systems never call KRA directly and never see KRA's field names, response codes, or the `cmcKey`. Our gateway owns the KRA session/branch registration, all master-data caching, and all retry/idempotency logic.

---

## 3. Tech Stack

| Layer | Choice | Notes |
|---|---|---|
| API framework | FastAPI (Python 3.11+) | async, Pydantic v2 models map 1:1 to KRA JSON schemas |
| HTTP client to KRA | `httpx.AsyncClient` | async, supports timeouts/retries |
| DB | PostgreSQL | transactional integrity for invoice numbering, idempotency |
| ORM | SQLAlchemy 2.0 (async) + Alembic migrations | |
| Task/queue | Celery or FastAPI `BackgroundTasks` + APScheduler for polling jobs; recommend **Celery + Redis** for retry/outbox at scale | |
| Auth (inbound, client→gateway) | OAuth2 client-credentials or per-client API key + HMAC signature | |
| Auth (outbound, gateway→KRA) | KRA `tin` + `bhfId` + `cmcKey` per branch (obtained via Device Initialization) | |
| Observability | structured logging (JSON), OpenTelemetry traces, Prometheus metrics | |
| Secrets | Vault / AWS Secrets Manager for `cmcKey` storage per branch | |

---

## 4. KRA Environments (from spec §1.2)

| Type | Base URL |
|---|---|
| Production | `https://etims-api.kra.go.ke/etims-api/` |
| Sandbox/Test | `https://etims-api-sbx.kra.go.ke/etims-api/` |

Interface format: JSON only. All endpoints below are relative to the base URL above and are called as `POST {base_url}{endpoint}`.

Config via environment variables:
```
KRA_ENV=sandbox|production
KRA_BASE_URL_PROD=https://etims-api.kra.go.ke/etims-api/
KRA_BASE_URL_SBX=https://etims-api-sbx.kra.go.ke/etims-api/
KRA_REQUEST_TIMEOUT_SECONDS=30
```

---

## 5. KRA Endpoint Catalogue (Adapter Layer)

The `KraAdapter` service class implements one method per KRA endpoint. All requests require `tin`, `bhfId`, and (after init) `cmcKey` in the body.

| # | KRA URL | Direction | Purpose | Our Adapter Method |
|---|---|---|---|---|
| 1 | `/selectInitOsdcInfo` | Send only | Device/branch initialization, returns `cmcKey` | `initialize_device()` |
| 2 | `/selectCodeList` | Get | Standard code tables (tax type, currency, etc.) | `get_code_list(last_req_dt)` |
| 3 | `/selectItemClsList` | Get | Item classification (UNSPSC-like) codes | `get_item_class_list(last_req_dt)` |
| 4 | `/selectCustomer` | Get | Taxpayer/customer lookup by PIN | `get_customer(custm_tin)` |
| 5 | `/selectBhfList` | Get | Branch list | `get_branch_list(last_req_dt)` |
| 6 | `/selectNoticeList` | Get | KRA notices | `get_notice_list(last_req_dt)` |
| 7 | `/saveBhfCustomer` | Send | Push branch customer info | `save_branch_customer(payload)` |
| 8 | `/saveBhfUser` | Send | Push branch user/employee account | `save_branch_user(payload)` |
| 9 | `/saveBhfInsurance` | Send | Push branch insurance info (pharmacy only) | `save_branch_insurance(payload)` |
| 10 | `/selectItemList` | Get | Pull registered items | `get_item_list(last_req_dt)` |
| 11 | `/saveItem` | Send | Push item master | `save_item(payload)` |
| 12 | *(saveItemComposition — see §5.1)* | Send | Push item BOM/composition | `save_item_composition(payload)` |
| 13 | `/selectImportItemList` | Get | Pull customs-declared import items | `get_import_item_list(last_req_dt)` |
| 14 | `/updateImportItem` | Send | Approve/reject import item | `update_import_item(payload)` |
| 15 | `/saveTrnsSalesOsdc` | Send | Push sales transaction + invoice | `save_sales_transaction(payload)` |
| 16 | `/selectTrnsPurchaseSalesList` | Get | Pull unregistered purchase-side of sales made by suppliers | `get_purchase_sales_list(last_req_dt)` |
| 17 | `/insertTrnsPurchase` | Send | Push/confirm purchase transaction | `save_purchase_transaction(payload)` |
| 18 | `/selectStockMoveList` | Get | Pull stock movement between branches | `get_stock_move_list(last_req_dt)` |
| 19 | `/insertStockIO` | Send | Push stock in/out event | `save_stock_io(payload)` |
| 20 | `/saveStockMaster` | Send | Push remaining stock quantity per item | `save_stock_master(payload)` |

> §5.1 Note: the spec text references a `saveComposition` method (class `ItemExcute`) but does not print an explicit `url:` line for it in the extracted text; treat the endpoint name as `/saveItemComposition` pending confirmation against KRA's live API docs/Swagger before go-live, and add a config flag to override the path per environment.

### 5.1 Common request envelope

Every request to KRA carries these fields at minimum (spec §3.3.x):

```json
{
  "tin": "A123456789Z",
  "bhfId": "00",
  "cmcKey": "<255-char communication key>",
  "...": "endpoint-specific fields"
}
```

`tin` (11 char), `bhfId` (2 char, `00` = head office, `01..n` = branch), `cmcKey` (255 char) are mandatory on every call except the initial `/selectInitOsdcInfo` (which uses `dvcSrlNo` instead of `cmcKey`, since the key does not exist yet).

### 5.2 Common response envelope

```json
{
  "resultCd": "000",
  "resultMsg": "It is succeeded",
  "resultDt": "20200226143124",
  "data": { }
}
```

`resultCd` = `"000"` is the only success code; anything else must be treated as an error and mapped per the table in §10.

---

## 6. Data Model Reference (extracted from spec §3.3, verbatim field names/types/lengths)

All models below become **Pydantic `BaseModel`** classes in `app/schemas/kra/*.py`. Field lengths and required flags are taken directly from the KRA spec so validation happens *before* we call KRA (fail fast, cheap).

### 6.1 Device Initialization — `/selectInitOsdcInfo`

**Request** (`DeviceVerificationReq`)
| Field | Type | Req | Len |
|---|---|---|---|
| tin | CHAR | Y | 11 |
| bhfId | CHAR | Y | 2 |
| dvcSrlNo | CHAR | Y | 100 |

**Response** (`DeviceVerificationRes.data`) returns `InitTaxpayer{tin, taxprNm, bsnsActv}`, `InitBranch{bhfId, bhfNm, bhfOpenDt, prvncNm, dstrtNm, sctrNm, locDesc, hqYn, mgrNm, mgrTelNo, mgrEmail}`, `InitDevice{dvcId, sdcId, mrcNo, cmcKey}`.

➡ **The gateway must persist `cmcKey`, `dvcId`, `sdcId`, `mrcNo` per `(tin, bhfId)` immediately** — this is the credential used for every subsequent call to that branch.

### 6.2 Code Information

- `CodeSearchReq` — `{tin, bhfId, cmcKey, lastReqDt}` → `CodeSearchRes.data.clsList[]` of `{cdCls, cdClsNm, cdClsDesc, userDfnNm1..3, useYn, dtlList:[{cd, cdNm, cdDesc, srtOrd, userDfnCd1..3, useYn}]}`.
- `CustSearchReq` — `{tin, bhfId, cmcKey, custmTin}` → `CustSearchRes.data.custList[]` of `{tin, taxprNm, taxprSttsCd, prvncNm, dstrtNm, sctrNm, locDesc}`.
- `NoticeSearchReq` — `{tin, bhfId, cmcKey, lastReqDt}` → `NoticeSearchRes.data.noticeList[]` of `{noticeNo, title, cont, dtlUrl, regrNm, regDt}`.

### 6.3 Item Information

- `ItemClsSearchReq` → `ItemClsSearchRes.data.itemClsList[]` of `{itemClsCd, itemClsNm, itemClsLvl, taxTyCd, mjrTgYn, useYn}`.
- `ItemSaveReq` (`/saveItem`) — required fields: `tin, bhfId, cmcKey, itemClsCd(10), itemCd(20, see §11 item-code format), itemTyCd(5), itemNm(200), orgnNatCd(5), pkgUnitCd(5), qtyUnitCd(5), taxTyCd(5), dftPrc(18,2), isrcAplcbYn(1), useYn(1), regrId(20), regrNm(60), modrId(20), modrNm(60)`; optional: `itemStdNm, btchNo, bcd, grpPrcL1..5, addInfo, sftyQty`.
- `ItemSearchReq` (`/selectItemList`) → returns full item list incl. `regBhfId`, `rraModYn` (whether KRA modified the classification code).

### 6.4 Branch Information

- `BhfSearchReq` → `BhfSearchRes.data.bhfList[]` — full branch master (address, manager contact, `hqYn`).
- `BhfCustSaveReq` (`/saveBhfCustomer`) — `custNo(9), custTin(11), custNm(60)` required; `adrs, telNo, email, faxNo, remark` optional.
- `BhfUserSaveReq` (`/saveBhfUser`) — `userId(20), userNm(60), pwd(255)` required.
- `BhfInsuranceSaveReq` (`/saveBhfInsurance`) — `isrccCd(10), isrccNm(100), isrcRt(NUMBER,3)` (pharmacy taxpayers only).

### 6.5 Import Item Information

- `ImportItemSearchReq` → `ImportItemSearchRes.data.itemList[]` — customs declaration fields: `taskCd, dclDe, itemSeq, dclNo, hsCd, itemNm, orgnNatCd, exptNatCd, pkg, pkgUnitCd, qty, qtyUnitCd, totWt, netWt, spplrNm, agntNm, invcFcurAmt, invcFcurCd, invcFcurExcrt`.
- `ImportItemUpdateReq` (`/updateImportItem`) — required: `taskCd, dclDe, itemSeq, hsCd, itemClCd, itemCd, imptItemsttsCd` (see §12.16 code list: 1 Unsent/2 Waiting/3 Approved/4 Cancelled).

### 6.6 Sales Information — `/saveTrnsSalesOsdc`

This is the highest-value/most complex payload. `TrnsSalesSaveWrReq`:

**Header** (required unless noted): `tin, bhfId, cmcKey, trdInvcNo(50), invcNo(38), orgInvcNo(38), rcptTyCd(5), salesSttsCd(5), cfmDt(14,yyyyMMddhhmmss), salesDt(8,yyyyMMdd), totItemCnt, taxblAmtA..E(18,2), taxRtA..E(7,2), taxAmtA..E(18,2), totTaxblAmt, totTaxAmt, totAmt, prchrAcptcYn(1), regrId, regrNm, modrId, modrNm`.
Optional: `custTin(11), custNm(60), pmtTyCd(5), stockRlsDt, cnclReqDt, cnclDt, rfdDt, rfdRsnCd, remark`.

**Sub-object `receipt` (`TrnsSalesSaveWrReceipt`)**: `rcptPbctDt(14, required)`; optional `custTin, custMblNo, trdeNm, adrs, topMsg, btmMsg`; required `prchrAcptcYn(1)`.

**Sub-object `itemList[]` (`TrnsSalesSaveWrItem`)**: required `itemSeq, itemCd(20), itemNm(200), pkgUnitCd(5), pkg(13,2), qtyUnitCd(5), qty(13,2), prc(18,2), splyAmt(18,2), dcRt(5,2), dcAmt(18,2), taxTyCd(5), taxblAmt(18,2), totTaxAmt(18,2), totAmt(18,2)`; optional `itemClsCd, bcd, isrccCd, isrccNm, isrcRt, isrcAmt`.

**Response `data`**: `{curRcptNo, totRcptNo, intrlData(26), rcptSign(16), sdcDateTime(14)}` — **must be persisted and returned to the originating client system** (this is the fiscal receipt proof).

### 6.7 Purchase Information

- `TrnsPurchaseSalesReq` (`/selectTrnsPurchaseSalesList`) → list of supplier-registered sales not yet matched to our purchases (`spplrTin, spplrBhfId, spplrInvcNo, rcptTyCd, pmtTyCd, cfmDt, salesDt, stockRlsDt, totItemCnt, taxblAmtA..E, taxRtA..E, taxAmtA..E, totTaxblAmt, totTaxAmt, totAmt, itemList[]`).
- `TrnsPurchaseSaveReq` (`/insertTrnsPurchase`) — mirrors the sales structure with `regTyCd` (Automatic/Manual, §12.13), `pchsTyCd` (Transaction Type, §12.9), `rcptTyCd` (Purchase Receipt Type: P=Purchase / R=Credit Note after Purchase), `pchsSttsCd` (Transaction Progress), `pchsDt`, plus `itemList[]` with `itemExprDt`.

### 6.8 Stock Information

- `StockMoveReq` (`/selectStockMoveList`) → inter-branch movement history.
- `StockIOSaveReq` (`/insertStockIO`) — required: `sarNo, orgSarNo, regTyCd, sarTyCd (see §12.15 Stock In/Out code), ocrnDt, totItemCnt, totTaxblAmt, totTaxAmt, totAmt, regrId, regrNm, modrId, modrNm`, plus `itemList[]` (`StockIoItemSaveReq`) each with `itemSeq, itemClsCd, itemNm, pkgUnitCd, pkg, qtyUnitCd, qty, prc, splyAmt, totDcAmt, taxblAmt, taxTyCd, taxAmt, totAmt`.
- `StockMasterSaveReq` (`/saveStockMaster`) — `itemCd, rsdQty (remaining quantity)`.

---

## 7. Code Tables to Cache Locally (spec §4)

Cache these in a `kra_code_lists` table, refreshed via the polling scheduler (§9), so client systems can validate against them before submission and so the gateway can map friendly enums to KRA codes:

| Table | Classification # | Notes |
|---|---|---|
| Tax Type | 04 | A=Exempt, B=16%, C=0%, D=Non-VAT, E=8% |
| Taxpayer Status | 15 | A=Active, D=Inactive |
| Product Type | 24 | 1=Raw Material, 2=Finished Product, 3=Service |
| Countries (Nation) | 05 | ISO-like 2-letter codes, ~250 rows (full list in spec §4.4) |
| Packaging Unit | 17 | ~52 codes (AM, BA, BC, … TN) |
| Unit of Quantity | 10 | ~43 codes (4B, AV, BA, … YRD) |
| Currency | 33 | ISO 4217, 179 codes |
| Transaction Type | 14 | C/N/P/T |
| Sales Receipt Type | 37 | S=Sale, R=Credit Note after Sale |
| Payment Method | 07 | 01 CASH … 07 OTHER |
| Transaction Progress | 11 | 01 Wait for Approval … 06 Transferred |
| Registration Type | 31 | A=Automatic, M=Manual |
| Purchase Receipt Type | 38 | P=Purchase, R=Credit Note after Purchase |
| Stock In/Out | 12 | 01–06 incoming, 11–16 outgoing |
| Import Item Status | 26 | 1 Unsent, 2 Waiting, 3 Approved, 4 Cancelled |
| Credit Note Reason | 32 | 01 Missing Quantity … 06 Refund |

The bulk `/selectCodeList` call returns **all** of the above except Countries/Packaging/Quantity/Currency which have their own dedicated tables in the spec appendix but are fetched the same way (they are still `cdCls` groups under the same endpoint per §3.3.2 description — confirm classification numbers against a live sandbox call since appendix numbering (`05`,`17`,`10`,`33`) differs from the §3.2 "code data" description; **do not hardcode assumptions, always trust what `/selectCodeList` returns and store `cdCls` as the key**).

---

## 8. Item Code Format (spec §4.19)

`itemCd` is mandatory & globally unique per taxpayer. Format: `KE2NTBA0000012`
- `KE` — Origin nation code (2 chars)
- `2` — Product type code (1 char)
- `NT` — Packaging unit code (2 chars)
- `BA` — Quantity unit code (2 chars)
- `0000012` — zero-padded sequence, 7 digits, increments from `0000001`

The gateway should own a `item_code_sequence` table (or Postgres sequence) per `(tin, bhfId, orgnNatCd, itemTyCd, pkgUnitCd, qtyUnitCd)` composite key and generate this code automatically whenever a client submits a new item without one, rather than trusting clients (like SAP) to generate KRA-compliant codes.

---

## 9. FastAPI Application Structure

```
app/
  main.py                     # FastAPI() app, routers, startup events
  core/
    config.py                 # Settings (pydantic-settings), KRA env, DB DSN
    security.py                # client auth (API key/OAuth2), KRA cmcKey vault access
  db/
    models.py                  # SQLAlchemy models
    session.py
  schemas/
    kra/                       # 1:1 Pydantic mirrors of KRA JSON (per §6)
      device.py
      codes.py
      item.py
      branch.py
      import_item.py
      sales.py
      purchase.py
      stock.py
    client/                    # simplified inbound DTOs for SAP/other ERPs
      item_in.py
      sales_in.py
      purchase_in.py
      stock_in.py
  services/
    kra_adapter.py              # httpx wrapper, one method per KRA endpoint (§5)
    cmc_key_manager.py          # device init + key rotation/lookup
    code_cache_service.py       # refresh & query cached code tables
    idempotency.py              # dedupe by client-supplied external ref
    mapping/                    # client DTO -> KRA schema mappers per ERP
      sap_mapper.py
      generic_mapper.py
  api/
    v1/
      endpoints/
        items.py                 # POST /v1/items          -> saveItem
        branches.py               # POST /v1/branches/customers, /users, /insurance
        sales.py                  # POST /v1/sales-invoices -> saveTrnsSalesOsdc
        purchases.py              # POST /v1/purchases      -> insertTrnsPurchase
                                   # GET  /v1/purchases/pending -> selectTrnsPurchaseSalesList (cached)
        stock.py                  # POST /v1/stock/movements -> insertStockIO
                                   # POST /v1/stock/master    -> saveStockMaster
        imports.py                 # GET  /v1/imports  (cached), POST /v1/imports/{id}/decision
        codes.py                   # GET  /v1/codes/{classification}  (served from cache, not KRA passthrough)
        device.py                  # POST /v1/devices/initialize (admin-only, one-time per branch)
      router.py
  workers/
    poller.py                    # scheduled jobs calling all "Get" KRA endpoints incrementally
    outbox_dispatcher.py         # retries failed KRA sends
  tests/
```

### 9.1 Example inbound endpoint (client → gateway → KRA)

```python
# app/api/v1/endpoints/sales.py
from fastapi import APIRouter, Depends, HTTPException
from app.schemas.client.sales_in import ClientSalesInvoice
from app.schemas.kra.sales import TrnsSalesSaveWrRes
from app.services.mapping.generic_mapper import map_sales_invoice
from app.services.kra_adapter import KraAdapter
from app.services.idempotency import ensure_idempotent
from app.core.security import get_current_client

router = APIRouter(prefix="/sales-invoices", tags=["sales"])

@router.post("", response_model=TrnsSalesSaveWrRes, status_code=201)
async def submit_sales_invoice(
    payload: ClientSalesInvoice,
    client=Depends(get_current_client),
    kra: KraAdapter = Depends(),
):
    await ensure_idempotent(client_id=client.id, external_ref=payload.external_invoice_ref)

    kra_req = await map_sales_invoice(client=client, payload=payload)   # branch lookup, cmcKey injection,
                                                                          # itemCd resolution, tax code mapping
    try:
        kra_res = await kra.save_sales_transaction(kra_req)
    except KraTransientError:
        await enqueue_retry("save_sales_transaction", kra_req)
        raise HTTPException(202, "Accepted, KRA submission queued for retry")

    await persist_receipt(client, payload.external_invoice_ref, kra_res)
    return kra_res
```

### 9.2 Client-facing DTOs

Client systems (SAP, POS, other ERPs) should **not** need to know KRA's field names (`taxblAmtA`, `pkgUnitCd`, etc.). Define simplified DTOs, e.g.:

```python
class ClientSalesInvoiceLine(BaseModel):
    sku: str
    description: str
    quantity: Decimal
    unit_of_measure: str          # mapped via UoM crosswalk table -> qtyUnitCd
    unit_price: Decimal
    discount_pct: Decimal = 0
    tax_category: str             # "STANDARD16" | "ZERO" | "EXEMPT" | "NONVAT" | "8PCT" -> taxTyCd

class ClientSalesInvoice(BaseModel):
    branch_code: str              # our internal branch id -> resolves (tin,bhfId,cmcKey)
    external_invoice_ref: str     # idempotency key, e.g. SAP billing doc number
    customer_pin: str | None
    customer_name: str | None
    payment_method: str           # "CASH" | "CREDIT" | ... -> pmtTyCd
    invoice_date: date
    lines: list[ClientSalesInvoiceLine]
```

A per-ERP "mapper" module (`sap_mapper.py`, `generic_mapper.py`) converts these into the exact KRA `TrnsSalesSaveWrReq` structure, resolving:
- branch → `(tin, bhfId, cmcKey)` from the `branches` table populated at device-init time
- `tax_category` → `taxTyCd` via the cached Tax Type code table
- `unit_of_measure` → `qtyUnitCd` via cached Unit of Quantity table
- `sku` → KRA `itemCd` via the `items` table (auto-created per §8 if unseen)
- computing the required derived totals (`taxblAmtA..E`, `taxAmtA..E`, `totTaxblAmt`, `totTaxAmt`, `totAmt`) bucketed by tax type A–E per spec.

---

## 10. Error Handling (spec §4.18 API Response Codes)

| Code | Meaning | Gateway behavior |
|---|---|---|
| 000 | Success | Persist result, return 200/201 to client |
| 001 | No search result | Treat as empty result set (not an error) for GET-style polling |
| 891–899 | Client-side transport/format errors (URL, header, body, method, status) | **Bug in our adapter** — log at ERROR, alert, do NOT retry blindly (fix payload first) |
| 900 | No header info | Config/auth bug — alert |
| 901 | Invalid device | `cmcKey`/device mismatch — trigger re-initialization workflow |
| 902 | Device already installed | Only during `/selectInitOsdcInfo`; treat as "already provisioned", fetch existing key from local store if present, else escalate to ops |
| 903 | Only OSCU device can be verified | Config error |
| 910–912 | Request parameter/body/method errors | 4xx back to client with details; do not forward to KRA again unmodified |
| 921 | Sales/sales-invoice not receivable (declared data missing) | Business-rule error, surface to client as 409 |
| 922 | Sales invoice only receivable after sales data received | Sequencing issue — our own outbox must send `sales` before `invoice` sub-objects (they're actually one payload per spec, so this implies partial-state on KRA side; queue and retry after backoff) |
| 990 | Max views exceeded | Rate-limit; backoff and retry per §9 polling cadence |
| 991–993 | Error during registration/modification/deletion | 5xx-class KRA-side error — retry with backoff |
| 994 | Overlapped data | Duplicate submission — treat as **success-equivalent** if our idempotency key already matches a prior success, else surface conflict |
| 995 | No downloaded file | N/A for our endpoints currently |
| 999 | Unknown error | Retry with exponential backoff, alert after N attempts |

All non-`000` results are stored in a `kra_call_log` table (endpoint, request payload hash, `resultCd`, `resultMsg`, `resultDt`, retry_count) for audit and support.

---

## 11. Incremental Sync / Polling Design (`lastReqDt` pattern)

Per spec §2.2 rule 3: *"To retrieve the latest data from eTIMS API server, OSCU shall send the 'Date and Time' on which the kind of data was finally received... TIS shall update the latest 'Date and Time' to the related data for the next update."*

Implementation:
- Table `kra_sync_state(tin, bhfId, resource, last_req_dt)` — one row per polled resource (`codes`, `item_cls`, `notices`, `items`, `branches`, `import_items`, `purchase_sales`, `stock_moves`).
- A Celery-beat/APScheduler job runs every N minutes per resource (configurable; e.g. codes every 6h, notices every 1h, import items every 15m, purchase-sales every 15m, stock moves every 15m):
  1. Read `last_req_dt` for `(tin, bhfId, resource)`.
  2. Call the corresponding KRA "Get" endpoint with that `lastReqDt`.
  3. Upsert returned rows into local cache tables.
  4. Set `last_req_dt = resultDt` (the KRA server's own result timestamp, per spec convention) — **never the client clock**, to avoid clock-skew gaps.
  5. On failure, do **not** advance `last_req_dt` (so the next run retries the same window).

---

## 12. Device Initialization & `cmcKey` Lifecycle

1. **Prerequisite (manual, one-time, per branch):** taxpayer applies for OSCU service via ETIMS Taxpayer Portal → Service Request → ETIMS, and KRA officer approves (spec §2.1). This happens outside our system.
2. **Provisioning endpoint** `POST /v1/devices/initialize` (admin/ops only, not exposed to SAP/ERP clients):
   - Input: `tin`, `bhfId`, and a generated `dvcSrlNo` (we control this — e.g. `"{env}-{tin}-{bhfId}-{uuid4}"`).
   - Calls KRA `/selectInitOsdcInfo`.
   - Persists `cmcKey`, `dvcId`, `sdcId`, `mrcNo`, plus the returned branch/taxpayer metadata, into `branches` table keyed by `(tin, bhfId)`.
   - `cmcKey` is stored **encrypted at rest** (KMS/Vault-backed column encryption), never logged, never returned in any client-facing API response.
3. All later adapter calls fetch `cmcKey` server-side from `branches` by internal `branch_code` — client systems only ever reference their own `branch_code`, never KRA identifiers directly.
4. If KRA returns `901 Invalid device`, the gateway flags the branch `status=NEEDS_REINIT` and alerts ops; automatic silent re-init is **not** recommended since it changes the trust anchor for a fiscal device.

---

## 13. Idempotency & Exactly-Once Semantics

Client systems (especially SAP, which frequently retries failed HTTP calls) must be protected from double-fiscalizing an invoice:

- Every mutating client endpoint requires an `external_ref` (e.g., SAP billing document number + item, or a client-generated UUID).
- Table `idempotency_keys(client_id, external_ref, endpoint, kra_response_json, created_at)` — unique constraint on `(client_id, external_ref, endpoint)`.
- On a repeat request with the same `external_ref`, the gateway returns the **previously stored KRA response** (including `invcNo`, `rcptSign`, etc.) with HTTP 200, instead of re-submitting to KRA — this also naturally handles KRA code `994 Overlapped data`.

---

## 14. Outbox Pattern for Reliability

Because KRA availability/latency is outside our control, mutating calls should not block the client's HTTP request indefinitely:

1. Client POSTs a business event → gateway validates + maps to KRA schema → **writes to `outbox` table in the same DB transaction as the business record** → returns `202 Accepted` with a tracking id (or `201` if we choose to await KRA synchronously for sales, since clients typically need the fiscal receipt number immediately — configurable per endpoint).
2. `outbox_dispatcher` worker picks up pending rows, calls `KraAdapter`, updates status (`SENT`, `FAILED`, `RETRYING`), applies exponential backoff (e.g., 30s, 2m, 10m, 1h, then alert).
3. Clients can poll `GET /v1/sales-invoices/{external_ref}/status` or receive a webhook callback once KRA fiscalization completes, if synchronous response isn't required.

> Recommendation: for **sales invoices** (fiscal receipts), prefer **synchronous** KRA call within the request (with a short timeout, e.g. 15s) since the client typically needs `rcptSign`/`curRcptNo` to print a compliant receipt immediately. For **master data** (items, branches, stock, purchases), prefer **async outbox** since there's no real-time printing requirement.

---

## 15. Client Authentication (Inbound)

- Each client system (SAP prod, SAP QA, third-party POS vendor, etc.) is registered in a `clients` table with a client_id, hashed API key or OAuth2 client-credentials, and an allow-list of `branch_code`s it may post for.
- Recommend OAuth2 client-credentials flow (`/oauth/token`) issuing short-lived JWT access tokens, validated via `fastapi.security.OAuth2PasswordBearer`/`python-jose`.
- Rate limiting per client (e.g., `slowapi`/Redis token bucket) to protect the gateway and to stay within any KRA-imposed volume limits (see code `990`).
- All inbound payloads logged (with PII/tax data access-controlled) for audit trail required by KRA compliance.

---

## 16. Sample SAP Integration Pattern

SAP typically won't call our REST API natively without middleware; two supported patterns:

1. **SAP PI/PO or CPI (Cloud Integration) as the transport layer**: SAP emits an IDoc (`INVOIC02`/custom) or OData call on billing document release → PI/PO transforms to our `ClientSalesInvoice` JSON → HTTPS POST to `/v1/sales-invoices`.
2. **SAP Business Add-In / custom ABAP report** calling our REST API directly via `cl_http_client` with OAuth2 client credentials, triggered on VF01 (invoice) save or a batch job.

Either way, SAP only needs to know our gateway's DTO shape (§9.2), not KRA's.

---

## 17. Database Schema (core tables)

```sql
CREATE TABLE branches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_code TEXT UNIQUE NOT NULL,      -- internal code exposed to clients
    tin CHAR(11) NOT NULL,
    bhf_id CHAR(2) NOT NULL,
    cmc_key TEXT NOT NULL,                  -- encrypted at rest
    dvc_id TEXT,
    sdc_id TEXT,
    mrc_no TEXT,
    status TEXT NOT NULL DEFAULT 'ACTIVE',  -- ACTIVE | NEEDS_REINIT | SUSPENDED
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (tin, bhf_id)
);

CREATE TABLE clients (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,                     -- e.g. "SAP-PROD", "POS-VENDOR-X"
    api_key_hash TEXT NOT NULL,
    allowed_branch_ids UUID[] NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id UUID REFERENCES branches(id),
    sku TEXT NOT NULL,                      -- client-side key
    item_cd TEXT NOT NULL,                  -- KRA-format code, see §8
    item_cls_cd TEXT NOT NULL,
    item_nm TEXT NOT NULL,
    tax_ty_cd CHAR(1) NOT NULL,
    dft_prc NUMERIC(18,2) NOT NULL,
    kra_synced_at TIMESTAMPTZ,
    UNIQUE (branch_id, sku)
);

CREATE TABLE kra_code_lists (
    cd_cls TEXT NOT NULL,
    cd TEXT NOT NULL,
    cd_nm TEXT,
    cd_desc TEXT,
    srt_ord INT,
    use_yn CHAR(1),
    refreshed_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (cd_cls, cd)
);

CREATE TABLE kra_sync_state (
    tin CHAR(11) NOT NULL,
    bhf_id CHAR(2) NOT NULL,
    resource TEXT NOT NULL,
    last_req_dt TEXT NOT NULL,
    PRIMARY KEY (tin, bhf_id, resource)
);

CREATE TABLE idempotency_keys (
    client_id UUID NOT NULL REFERENCES clients(id),
    external_ref TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    kra_response JSONB,
    created_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (client_id, external_ref, endpoint)
);

CREATE TABLE outbox (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    endpoint TEXT NOT NULL,
    branch_id UUID REFERENCES branches(id),
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING|SENT|FAILED|RETRYING
    attempts INT NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE kra_call_log (
    id BIGSERIAL PRIMARY KEY,
    branch_id UUID REFERENCES branches(id),
    endpoint TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    result_cd TEXT,
    result_msg TEXT,
    result_dt TEXT,
    retry_count INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE sales_invoices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id UUID REFERENCES branches(id),
    external_ref TEXT NOT NULL,
    kra_invc_no BIGINT,
    kra_cur_rcpt_no BIGINT,
    kra_tot_rcpt_no BIGINT,
    kra_rcpt_sign TEXT,
    kra_intrl_data TEXT,
    kra_sdc_datetime TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING|FISCALIZED|FAILED
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (branch_id, external_ref)
);
```

---

## 18. Client-Facing REST API Summary

| Method & Path | Purpose | KRA endpoint(s) invoked |
|---|---|---|
| `POST /v1/devices/initialize` *(admin only)* | Register/activate a branch device | `/selectInitOsdcInfo` |
| `GET /v1/codes/{classification}` | Fetch cached code table (e.g. `tax-type`, `currency`) | served from local cache, refreshed via `/selectCodeList` |
| `POST /v1/items` | Create/update item master | `/saveItem` (+ `/selectItemList` sync for reconciliation) |
| `POST /v1/items/{sku}/composition` | Push BOM/composition | `saveItemComposition` |
| `GET /v1/items` | List items known to gateway | local cache (synced via `/selectItemList`) |
| `POST /v1/branches/customers` | Push branch customer | `/saveBhfCustomer` |
| `POST /v1/branches/users` | Push branch user | `/saveBhfUser` |
| `POST /v1/branches/insurance` | Push branch insurance info | `/saveBhfInsurance` |
| `GET /v1/imports` | List pending customs import items | local cache (synced via `/selectImportItemList`) |
| `POST /v1/imports/{taskCd}/decision` | Approve/reject import item | `/updateImportItem` |
| `POST /v1/sales-invoices` | Submit a fiscal sales invoice (sync, returns receipt) | `/saveTrnsSalesOsdc` |
| `GET /v1/sales-invoices/{external_ref}` | Fetch fiscalization status/receipt | local cache |
| `GET /v1/purchases/pending` | List supplier-declared sales awaiting our purchase confirmation | local cache (synced via `/selectTrnsPurchaseSalesList`) |
| `POST /v1/purchases` | Submit/confirm a purchase transaction | `/insertTrnsPurchase` |
| `GET /v1/stock/movements` | List inter-branch stock movements | local cache (synced via `/selectStockMoveList`) |
| `POST /v1/stock/movements` | Submit stock in/out event | `/insertStockIO` |
| `POST /v1/stock/master` | Submit remaining stock quantity for an item | `/saveStockMaster` |
| `GET /v1/notices` | List KRA notices | local cache (synced via `/selectNoticeList`) |
| `GET /v1/health` | Liveness/readiness, incl. last successful KRA poll timestamp | n/a |

All `POST` endpoints require the OAuth2/API-key auth described in §15 and an `external_ref`/idempotency key per §13.

---

## 19. Non-Functional Requirements

- **Availability:** gateway should be deployable multi-instance behind a load balancer; outbox/poller workers must use DB-level locking (`SELECT ... FOR UPDATE SKIP LOCKED`) to avoid double-processing.
- **Security:** TLS everywhere; `cmcKey` and other credentials encrypted at rest and never exposed via API/logs; least-privilege DB roles; audit log of every KRA call retained per KRA/finance record-retention requirements.
- **Compliance/traceability:** every fiscal invoice's KRA receipt fields (`rcptSign`, `intrlData`, `curRcptNo`, `totRcptNo`, `sdcDateTime`) must be immutable once written (append-only or WORM storage recommended for the `sales_invoices` table).
- **Performance targets:** p95 < 3s for synchronous `/v1/sales-invoices` calls (excluding KRA-side latency spikes); async endpoints should acknowledge in < 300ms.
- **Testing:** contract tests against KRA **sandbox** (`etims-api-sbx.kra.go.ke`) for every adapter method before promoting to production; golden-file tests comparing our generated JSON to the exact spec samples in §3.3.x of the KRA document.

---

## 20. Open Items to Confirm with KRA / Live Sandbox Before Build

1. Exact URL path for "Save Item Composition" (`saveComposition` method) — not printed explicitly in the extracted spec text.
2. Whether `/selectCodeList` truly returns Nation/Packaging/Quantity/Currency (§4.4–4.8) under the same `cdCls` mechanism as Tax Type/Payment Method, or whether those larger reference tables are distributed as a static download — confirm against a live sandbox response before hardcoding.
3. Rate limits behind response code `990` (max views exceeded) — get concrete numbers from KRA to size the polling scheduler intervals in §11.
4. Confirm whether KRA requires mutual TLS or IP allow-listing in production in addition to the JSON body credentials.

---

*This specification was derived directly from the "Online Sales Control Unit (OSCU) Requirements & Communication Protocols v2.0" document (KRA Domestic Taxes Department, April 2023). Field names, lengths, and endpoint URLs are taken verbatim from that source; anything not explicitly stated in the source (e.g., exact composition-save URL, rate limits) is flagged in §20 rather than assumed.*
