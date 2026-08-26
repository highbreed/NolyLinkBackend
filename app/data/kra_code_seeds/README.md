# KRA static code-table seed data (fallback for spec §20 item 2)

`app/services/code_cache_service.py`'s `load_static_seed_fallback()` reads
`{slug}.json` from this directory — one file per large reference table
(`nation.json`, `packaging-unit.json`, `quantity-unit.json`, `currency.json`),
each a JSON list of `{"cd", "cdNm", "cdDesc", "srtOrd", "useYn"}` objects — only
if `KRA_LARGE_CODE_TABLES_INCLUDED_IN_CODE_LIST=false` (i.e. sandbox testing has
shown these four classifications are *not* returned via `/selectCodeList`).

**No seed files are checked in yet.** `KRA_OSCU_FastAPI_Integration_Spec.md`
(the only source document available while building this) doesn't reproduce
KRA's actual appendix tables (§4.4–4.8 of the *original* KRA protocol
document) — it only gives truncated examples like "Packaging Unit: ~52 codes
(AM, BA, BC, … TN)", with no `cdNm`/`cdDesc` attached to any of them.
Fabricating ~250 country / ~52 packaging / ~43 quantity / 179 currency codes
from that would mean inventing fiscal reference data, so the loader is real
but the data isn't: it logs a warning and skips any classification with no
seed file here, rather than silently running with fake codes.

To make this fallback usable, drop the real files here (sourced from KRA's
official appendix or a confirmed sandbox response) — no code changes needed.
