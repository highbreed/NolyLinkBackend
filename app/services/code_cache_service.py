"""Refresh & query cached KRA code tables via lastReqDt incremental sync (spec §7, §11).

Two things spec §20 item 2 / §7 explicitly flags as unconfirmed and NOT to hardcode:
  1. The classification-number -> cdCls mapping below (CLASSIFICATION_SLUGS) is taken from
     spec §7's table, but that table itself says the appendix numbering (05/17/10/33 for
     Nation/Packaging/Quantity/Currency) may differ from KRA's actual /selectCodeList
     response. It's offered as a convenience lookup for GET /v1/codes/{classification}, not
     as ground truth — querying by the raw cdCls /selectCodeList actually returns always
     works too (see resolve_classification).
  2. Whether Nation/Packaging/Quantity/Currency come back through /selectCodeList at all, or
     are distributed separately, is gated by settings.kra_large_code_tables_included_in_code_list
     (default True, matching the spec text's own reading) so it's a one-line fix once sandbox
     access confirms it either way — see load_static_seed_fallback below.
"""

import json
import logging
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Branch, KraCodeList
from app.schemas.kra.codes import CodeSearchReq
from app.services import sync_framework
from app.services.kra_adapter import KraAdapter

logger = logging.getLogger(__name__)

RESOURCE = "codes"

# spec §7's table. Values are cdCls codes as documented there — see the module docstring's
# caveat #1 before treating these as authoritative.
CLASSIFICATION_SLUGS: dict[str, str] = {
    "tax-type": "04",
    "taxpayer-status": "15",
    "product-type": "24",
    "nation": "05",
    "packaging-unit": "17",
    "quantity-unit": "10",
    "currency": "33",
    "transaction-type": "14",
    "sales-receipt-type": "37",
    "payment-method": "07",
    "transaction-progress": "11",
    "registration-type": "31",
    "purchase-receipt-type": "38",
    "stock-in-out": "12",
    "import-item-status": "26",
    "credit-note-reason": "32",
}

# The four classifications spec §20 item 2 singles out as possibly not coming back through
# /selectCodeList's normal mechanism.
LARGE_CODE_TABLE_SLUGS = frozenset({"nation", "packaging-unit", "quantity-unit", "currency"})

SEED_DIR = Path(__file__).resolve().parent.parent / "data" / "kra_code_seeds"


def resolve_classification(classification: str) -> str:
    """Accept either a friendly slug (spec §7) or a raw cdCls code directly — the latter
    always works since kra_code_lists is keyed by whatever cdCls /selectCodeList returns.
    """
    return CLASSIFICATION_SLUGS.get(classification, classification)


async def refresh_code_list(db: AsyncSession, kra: KraAdapter, branch: Branch) -> None:
    """Incremental sync via /selectCodeList (spec §7, §11). On any KraError this simply
    doesn't advance last_req_dt, so the next run retries the same window.
    """
    last_req_dt = await sync_framework.get_last_req_dt(db, branch.tin, branch.bhf_id, RESOURCE)
    req = CodeSearchReq(
        tin=branch.tin, bhfId=branch.bhf_id, cmcKey=branch.cmc_key, lastReqDt=last_req_dt
    )
    res = await kra.get_code_list(req, branch_id=branch.id)

    if res.data is not None:
        for cls in res.data.clsList:
            for detail in cls.dtlList:
                await _upsert_code(
                    db,
                    cd_cls=cls.cdCls,
                    cd=detail.cd,
                    cd_nm=detail.cdNm,
                    cd_desc=detail.cdDesc,
                    srt_ord=detail.srtOrd,
                    use_yn=detail.useYn,
                )

    await sync_framework.advance_last_req_dt(db, branch.tin, branch.bhf_id, RESOURCE, res.resultDt)

    if not get_settings().kra_large_code_tables_included_in_code_list:
        await load_static_seed_fallback(db)


async def _upsert_code(
    db: AsyncSession,
    cd_cls: str,
    cd: str,
    cd_nm: str | None,
    cd_desc: str | None,
    srt_ord: int | None,
    use_yn: str | None,
) -> None:
    stmt = pg_insert(KraCodeList).values(
        cd_cls=cd_cls, cd=cd, cd_nm=cd_nm, cd_desc=cd_desc, srt_ord=srt_ord, use_yn=use_yn
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[KraCodeList.cd_cls, KraCodeList.cd],
        set_={
            "cd_nm": stmt.excluded.cd_nm,
            "cd_desc": stmt.excluded.cd_desc,
            "srt_ord": stmt.excluded.srt_ord,
            "use_yn": stmt.excluded.use_yn,
            "refreshed_at": func.now(),
        },
    )
    await db.execute(stmt)


async def load_static_seed_fallback(db: AsyncSession) -> None:
    """Fallback for spec §20 item 2 if sandbox testing shows Nation/Packaging/Quantity/
    Currency are NOT returned via /selectCodeList — seeded from the tables the build prompt
    says are "printed in spec §4.4-4.8".

    Those tables are NOT actually reproduced in KRA_OSCU_FastAPI_Integration_Spec.md (only a
    truncated example like "AM, BA, BC, ... TN" for Packaging Unit, with no names/descriptions
    attached) — inventing ~250 country / ~52 packaging / ~43 quantity / 179 currency codes
    would mean fabricating fiscal reference data, which is worse than not having it. This
    function is therefore real plumbing over intentionally empty seed files: it loads and
    upserts whatever JSON lands in app/data/kra_code_seeds/{slug}.json (a list of
    {"cd", "cdNm", "cdDesc", "srtOrd", "useYn"} objects, one file per LARGE_CODE_TABLE_SLUGS
    entry), and logs a warning per classification with no seed file yet.
    """
    for slug in sorted(LARGE_CODE_TABLE_SLUGS):
        seed_file = SEED_DIR / f"{slug}.json"
        if not seed_file.exists():
            logger.warning(
                "No static seed file for %r at %s — populate it from KRA's official code "
                "appendix before relying on this fallback.",
                slug,
                seed_file,
            )
            continue

        cd_cls = resolve_classification(slug)
        rows = json.loads(seed_file.read_text())
        for row in rows:
            await _upsert_code(
                db,
                cd_cls=cd_cls,
                cd=row["cd"],
                cd_nm=row.get("cdNm"),
                cd_desc=row.get("cdDesc"),
                srt_ord=row.get("srtOrd"),
                use_yn=row.get("useYn"),
            )


async def get_codes(db: AsyncSession, classification: str) -> list[KraCodeList]:
    """Serves GET /v1/codes/{classification} from the local cache — never a KRA passthrough."""
    cd_cls = resolve_classification(classification)
    result = await db.execute(
        select(KraCodeList).where(KraCodeList.cd_cls == cd_cls).order_by(KraCodeList.srt_ord)
    )
    return list(result.scalars().all())
