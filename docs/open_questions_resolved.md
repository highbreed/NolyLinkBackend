# Open questions from spec §20 — status

## Item 2: Do Nation/Packaging/Quantity/Currency come back via `/selectCodeList`?

**Status: resolved 2026-08-31, from the source document itself** — not from a live sandbox
call (still never made), but from finally obtaining KRA's actual published
`OSCU_Specification_Document_v2.0.pdf` (see `docs/kra_specs/README.md`). Its §4 (Code
Definition) lists Nation (`cdCls 05`), Packaging Unit (`17`), Unit of Quantity (`10`), and
Currency (`33`) in the exact same chapter, same format, as Tax Type/Product Type/Payment
Method — confirming they're `/selectCodeList` results like everything else, not a separately
distributed static table. `Settings.kra_large_code_tables_included_in_code_list`'s default
(`True`) was the right guess.

**Still not done:** the actual seed data. §4.4–4.8 of that PDF contains the full real tables
(~250 country codes, ~52 packaging units, ~43 quantity units, 179 currency codes) — extracting
them into `app/data/kra_code_seeds/{slug}.json` is a mechanical but sizeable follow-up task,
not yet done.

<details>
<summary>Original (superseded) note, kept for context</summary>

No real KRA sandbox credentials (`tin`/`bhfId`/`dvcSrlNo`) have been provided to this build
(see the build prompt's "Ask before assuming" #2), so no live call to `/selectCodeList` has
actually been made against KRA. This question cannot be answered from the spec document alone
— the spec text itself says the appendix numbering (05/17/10/33) "differs from the §3.2 'code
data' description" and explicitly says not to hardcode an assumption here.

</details>

**What's implemented instead**, so this can be corrected with a one-line config change once
someone runs it against sandbox (see `app/services/code_cache_service.py`):

- `Settings.kra_large_code_tables_included_in_code_list` (default `True`, matching the spec
  text's own reading that these are "still `cdCls` groups under the same endpoint") gates
  whether `refresh_code_list()` also runs `load_static_seed_fallback()` after every sync.
- `load_static_seed_fallback()` is real, working code — it upserts whatever JSON it finds in
  `app/data/kra_code_seeds/{slug}.json` — but **no seed files are checked in**. The build
  prompt asks for this to be "seeded from the tables printed in spec §4.4–4.8", but those
  tables (KRA's actual ~250 country / ~52 packaging / ~43 quantity / 179 currency codes)
  aren't reproduced anywhere in `KRA_OSCU_FastAPI_Integration_Spec.md`, the only source
  document attached to this build — only truncated examples with no names/descriptions
  (e.g. "Packaging Unit: AM, BA, BC, … TN"). Fabricating the missing codes and names would
  mean inventing fiscal reference data, so the loader logs a warning and skips any
  classification with no seed file rather than running with invented data.

**To close this out:** either (a) run device init + a code-list poll against sandbox once
real credentials are available and observe whether `clsList` includes `cdCls` 05/17/10/33,
or (b) supply KRA's actual appendix tables (or the original protocol document, which the
build prompt calls "the original document") so real seed files can be written. Either input
requires zero code changes — see `app/data/kra_code_seeds/README.md`.

## Items 1, 3, 4

Not yet reached — item 1 (item-composition save URL) is tracked as a config flag in
`Settings.kra_item_composition_endpoint` (Phase 3). Items 3 (rate limits behind `990`) and 4
(mTLS/IP allow-listing in production) aren't relevant until later phases (polling scheduler
sizing, and production hardening respectively).
