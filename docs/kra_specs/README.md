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
