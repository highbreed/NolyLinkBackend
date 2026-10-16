# KRA source specification documents

Two official KRA documents, both titled "v2.0, April 2023," downloaded 2026-08-31 from
kra.go.ke and checked in here as the actual primary source — everything in
`KRA_OSCU_FastAPI_Integration_Spec.md` (the repo root) should trace back to one of these, not
the other way around.

## `OSCU_Specification_Document_v2.0.pdf`

**"Online Sales Control Unit (OSCU) Requirements & Communication Protocols v2.0"** — this is
the real source `KRA_OSCU_FastAPI_Integration_Spec.md` was derived from (title matches its own
citation exactly). Confirms, field-for-field, endpoint-for-endpoint:

- Base URLs: `https://etims-api.kra.go.ke/etims-api/` (prod), `https://etims-api-sbx.kra.go.ke/etims-api/` (test) — JSON only.
- Every endpoint path this gateway calls (`/selectInitOsdcInfo`, `/saveTrnsSalesOsdc`, etc.) — §3.3.
- The `{resultCd, resultMsg, resultDt, data}` envelope and the full `KraError` code table (000,
  001, 891–899, 900–903, 910–912, 921–922, 990–995, 999) — §4.18, matches
  `app/services/kra_adapter.py`'s `raise_for_result()` exactly.
- §4 (Code Definition) contains the **full data tables** for every `cdCls` this gateway caches
  — including Nation (`05`), Packaging Unit (`17`), Unit of Quantity (`10`), and Currency
  (`33`), confirming they come through `/selectCodeList` exactly like Tax Type/Product Type do.
  This resolves the open question in `docs/open_questions_resolved.md` that's stood
  unconfirmed since Phase 5 — the actual seed data for
  `app/data/kra_code_seeds/{slug}.json` is in this PDF's §4.4–4.8 and hasn't been extracted
  into seed files yet.

**One real bug this caught and fixed** (2026-08-31): §3.3.1's JSON sample shows
`/selectInitOsdcInfo`'s response nesting taxpayer/branch/device fields together under a single
flat `data.info` object — not three separate `data.{taxpayer,branch,device}` sub-objects as
`app/schemas/kra/device.py` originally guessed (that guess was explicitly flagged as
unconfirmed in the file's own docstring at the time). Fixed to match the confirmed shape.

## `TIS-OSCU-VSCU-v2.0.pdf`

**"Technical Specification of TIS for OSCU/VSCU v2.0"** — a *different* document covering
**VSCU** specifically (and OSCU's on-premise variant). Describes a completely different
integration model: **XML** command/response messages (`<PIN>`, `<CMD>`, `<DATA>`, `<STATUS>`)
between the TIS and a *local* OSCU/VSCU software module — verb-based commands
(`SEND_RECEIPT`, `ID_REQUEST`, `STATUS`, ...), single-letter `P`/`E` status, small numeric
error codes (`00`–`99`). None of the JSON REST paths, envelope, or result codes above appear
anywhere in this document.

**This means the assumption `device_type="VSCU"` was built on — that VSCU shares OSCU's
exact request/response contract, only the host differs — is wrong.** VSCU needs a genuinely
different integration (this XML/CMD protocol), not just `KraAdapter._resolve_base_url()`
pointing at a different host. `app/services/kra_adapter.py` and `app/schemas/kra/*` currently
only implement the OSCU (JSON) side; nothing in this codebase speaks the VSCU (XML) protocol
yet. Confirmed 2026-08-31 by testing a real device that identifies as "the sandbox VSCU we're
trying to test" — it responds to neither protocol at any endpoint tried, so it's separately
unclear whether that specific device is even a working implementation of either.

**Not yet built**: a VSCU adapter speaking this XML/CMD protocol. Scoping that (and figuring
out what transport/path a specific VSCU vendor implementation actually exposes it on, since
the spec itself leaves that vendor-defined) is future work, not done as part of this OSCU
fix.

## VSCU jar reverse-engineering: root cause confirmed (2026-08-31)

The remote sandbox device (`192.168.1.10:8088`) 404s on every JSON REST path this gateway or
the decompiled jar suggests. To rule out "stale/misconfigured remote instance" as the
explanation, KRA's own `vscu-v2.0.21.jar` (from `vscu-v2.0.21 Sandbox.zip`, not committed to
this repo — see `vscu_jar_file/`) was run **locally** in this sandbox with a portable Temurin
17 JDK. Same result: `POST /initializer1/selectInitInfo` 404s on a completely fresh instance,
seconds after a clean startup with no errors in the log.

**Root cause, found via `javap` on the decompiled `VsdcApplication.class`:**

```
@SpringBootApplication(scanBasePackages = {
    "com.kra.vsdc.config.client", "com.kra.vsdc.config.util",
    "com.kra.vsdc.execute.offline", "com.kra.vsdc.manage", "com.kra.vsdc.excute",
    "com.kra.skmm.config.security", "com.kra.skmm.manager", "com.kra.skmm.config.util"
})
```

`com.kra.vsdc.controler` — the package holding every REST controller with real,
correctly-annotated `@RestController`/`@RequestMapping` routes (`Initiolization` →
`/initializer1/selectInitInfo`, `SaveItems` → `/items1/saveSales`, `SaveSales` →
`/trnsSales1/saveSales`, plus `Transmission`, `Setup`, `PuchaseSave`) — **is not in that
list**. Spring Boot's component scan never sees these classes, so none of them become beans
and none of their `@PostMapping` routes ever register, regardless of `exec_mode`, `profile`,
or any other `vscu.properties` setting. Confirmed with `javap -v`, not just `strings` —
verified this is the actual `RuntimeVisibleAnnotations` value, not a guess.

The only things this jar actually exposes are: Spring Data REST's auto-CRUD resources
(`/{profile}/initiolises`, `/{profile}/transactions` — business-logic-free, direct table
access) and whatever's in the 8 scanned packages (scheduling, offline resend, config/util —
no controllers).

**Conclusion**: this is a packaging property of the distributed sandbox jar itself, not a
misconfigured or stale remote device. The `/initializer1/...` etc. paths are real code that
this specific build simply never turns on. A VSCU adapter cannot be built against this jar's
REST surface as currently shipped — either KRA needs to ship a build with the intended
component scan, or the actual integration contract is something else entirely (e.g. the
XML/CMD protocol from the other spec PDF above, which this jar doesn't appear to implement
either — no XML parsing classes were found in `com.kra.vsdc.controler`).

## Correction (2026-09-01): the *live* REST surface is `com.kra.vsdc.excute`, not `.controler`

The conclusion above was wrong in a specific, fixable way: it only checked
`com.kra.vsdc.controler` (the misspelled-with-one-`l` package holding `Initiolization`,
`SaveSales`, etc., confirmed excluded from `@SpringBootApplication(scanBasePackages=...)`) and
stopped there. It missed a *second*, differently-misspelled package that the same scan list
explicitly includes: `com.kra.vsdc.excute` (dropped `e`, not `com.kra.vsdc.execute` — that
third spelling is the DTOs-only `.execute.model`/`.execute.offline` packages, also scanned but
holding no controllers).

Decompiling `com.kra.vsdc.excute` with CFR (`org.benf:cfr:0.152`, a clean decompile, no
"could not structure code" warnings on any of the 19 classes there) confirms every class in it
**is** a real `@RestController` with a working `@RequestMapping`, and a live local instance of
this jar answers on all of them. Verified endpoint list (grepped every `@GetMapping`/
`@PostMapping` across all 19 controller classes):

```
GET  /main/selectServerTime
POST /initializer/selectInitInfo | /selectSequences | /updateSequences
POST /test/echoTest
POST /code/selectCodes
POST /itemClass/selectItemsClass
POST /customers/selectCustomer
POST /items/selectItems | /saveItems | /saveItemComposition
POST /branches/selectBranches | /saveBrancheUsers | /saveBrancheInsurances | /saveBrancheCustomers
POST /trnsSales/saveSales | /selectInvoiceDetails | /selectTrnsSalesList
POST /trnsPurchase/savePurchases | /selectTrnsPurchaseSales
POST /imports/selectImportItems | /updateImportItems
POST /stock/selectStockItems | /saveStockItems
POST /stockMaster/saveStockMaster
POST /notices/selectNotices
POST /reports/saveZReports | /checkZReport
```

This means the "V2.0.21 sandbox VSCU is unbuildable" conclusion above does not hold — there is
a working local JSON REST API. But it's still **not** "OSCU's contract at a different host"
(the assumption `Branch.device_type`/`kra_base_url_vscu_*` were built on, itself already
flagged wrong above): `com.kra.vsdc.excute`'s controllers proxy to real KRA using their own
distinct DTOs (`com.kra.vsdc.execute.model.*` — e.g. `TrnsSalesSaveWrReq`, `InitInfoReq`, not
OSCU's `app/schemas/kra/sales.py` shapes), and several of them do real local-device work before
ever calling KRA — sequence management, receipt signing in `exec_mode=OFFLINE`
(`TrnsSalesExcute.saveTrnsSales` signs locally and rewrites a 894 into a synthetic 000 once the
transaction is queued for its own resend scheduler), and stripping `cmcKey`/`signKey`/
`intrlKey` out of `/initializer/selectInitInfo`'s response before ever handing it back to us
(confirmed in `InitializeVsdcExcute.selectInitInfo`).

The real VSCU integration layer built against this (Phase 1: foundation, Phase 2: init/health/
sequences/echo) lives at `app/integrations/vscu/` — see its module docstrings for exactly which
claims here are decompile-verified vs. still-assumed. `docs/kra_specs/README.md`'s own
`TIS-OSCU-VSCU-v2.0.pdf` XML/CMD section above describes a genuinely different, real KRA spec
document — it just isn't what this specific jar exposes locally; that document may still be
the right reference for a different vendor's VSCU hardware/software.

## Follow-up fix (2026-09-02): device *registration* itself still routed VSCU through OSCU's KRA call

The correction above was written into `Branch`'s own docstring at the time, but one call site
was missed: `app/services/cmc_key_manager.py`'s `initialize_device` — the function behind both
`POST /v1/devices/initialize` and (later) `POST /v1/customers/onboard` — still called
`KraAdapter.initialize_device`, which POSTs to `/selectInitOsdcInfo` at
`KRA_BASE_URL_VSCU_{SBX,PROD}`, for `device_type="VSCU"` too. That endpoint list in the
correction above is the authority here: there is no `/selectInitOsdcInfo`-equivalent among the
19 confirmed `com.kra.vsdc.excute` controllers — VSCU's *only* initialization call is
`POST /initializer/selectInitInfo` against the local VSCU app, already correctly implemented as
`VSCUInitializationService.initialize`.

Fixed by giving `device_type="VSCU"` its own path in `cmc_key_manager.initialize_device`
(`_register_vscu_branch_locally`): it never calls real KRA at all — the `Branch` row is created
locally from admin-supplied `(branch_code, tin, bhfId)`, with `cmc_key=None` (now a nullable
column — migration `878fe0715698`) since the local VSCU app owns its own KRA trust anchor
internally and never exposes it to this gateway. `KRA_BASE_URL_VSCU_SBX`/`_PROD` and
`Settings.kra_base_url_for()`'s VSCU branch are consequently dead code in every real code path
today (kept only in case a genuinely VSCU-relevant KRA-hosted call is ever confirmed to exist —
see `KraAdapter._resolve_base_url`'s docstring).

## Follow-up finding (2026-09-02): `/initializer/selectInitInfo` is not re-callable — 902 means "already installed", recover via selectSequences

Confirmed against a real device (tin `P052058389S`, bhfId `01`, `192.168.1.10:8088`): once a
(tin, bhfId) pair has completed `/initializer/selectInitInfo` successfully, calling it again
for the same pair does not repeat the `InitInfoVO` payload — it responds
`resultCd=902 resultMsg="This device is installed"`. This was initially mistaken for a bug (it
surfaced as `VSCUValidationError`, "902" being in `KRA_AUTH_RANGE`, i.e. "malformed request, do
not retry") — it is in fact the local VSCU app correctly refusing to re-initialize.

Also confirmed on the same real device: `/initializer/selectSequences`, called with the exact
same `{tin, bhfId, dvcSrlNo}` body, returns the identical `InitInfoVO`-shaped payload
(`sdcId`/`mrcNo`/`dvcId`/sequence numbers/`cmcKey: null`) for an already-initialized device —
this is the correct read for "what state is this device already in", not `selectInitInfo`
again.

`VSCUInitializationService.initialize` now catches this specific case (902 on
`/initializer/selectInitInfo`) and falls back to `select_sequences` with the same
`base_url_override` bootstrap path `select_init_info` itself uses (no `VscuDeviceState` row
exists yet at this point), persisting whatever it returns instead of raising. Every other code
in `KRA_AUTH_RANGE` (900-901, 903) still raises `VSCUValidationError` as before — 901 was also
separately confirmed on the same device ("It is not valid device", for a `bhfId` genuinely not
provisioned on that hardware) and that one really is a request-validation failure, not a
recoverable state.

Also fixed in the same session: `InitInfoVO`'s non-identity fields (`taxprNm`, `bhfNm`,
`locDesc`, etc.) were required `str` and rejected a real device's `locDesc: null` on an
otherwise-successful response — see `app/integrations/vscu/schemas/init.py`'s docstring. And a
systemic bug where 11 call sites built error messages from `exc.context.result_cd`/
`result_msg` directly, producing `"VSCU error None: None"` for `VSCUConfigurationError`/
`VSCUTransportError` (which never carry those, by design) — fixed to use `str(exc)`
everywhere, which `VSCUError.__init__` already formats correctly for every subtype.
