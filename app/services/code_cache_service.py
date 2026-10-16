"""Refresh & query cached KRA code tables via lastReqDt incremental sync (spec §7, §11).

Two things spec §20 item 2 / §7 explicitly flags as unconfirmed and NOT to hardcode:
  1. Most of the classification-number -> cdCls mapping below (CLASSIFICATION_SLUGS) is taken
     from spec §7's table, but that table itself says the appendix numbering (05/17/10/33 for
     Nation/Packaging/Quantity/Currency) may differ from KRA's actual /selectCodeList
     response. It's offered as a convenience lookup for GET /v1/codes/{classification}, not
     as ground truth — querying by the raw cdCls /selectCodeList actually returns always
     works too (see resolve_classification). The entries marked "not from spec §7" below are a
     different, stronger kind of confirmed: taken directly from KRA's own cdClsNm on a real
     synced response (see KraCodeClassification / _upsert_code_class), not from the spec doc.
  2. Whether Nation/Packaging/Quantity/Currency come back through /selectCodeList at all, or
     are distributed separately, is gated by settings.kra_large_code_tables_included_in_code_list
     (default True, matching the spec text's own reading) so it's a one-line fix once sandbox
     access confirms it either way — see load_static_seed_fallback below.
"""

import json
import logging
from pathlib import Path
from typing import NamedTuple

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Branch, KraCodeClassification, KraCodeList
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
    # Not from spec §7 (which never lists these cd_cls values at all) — added 2026-09-18 from
    # KRA's own cdClsNm on a real synced response (KraCodeClassification, captured by
    # _upsert_code_class below), so these are KRA-confirmed, not guessed at from code shape.
    "branch-status": "09",  # cdClsNm "Branch Status"
    "purchase-status": "34",  # cdClsNm "Purchase Status" — cd_cls=11 ("transaction-progress")
    # is the identical 6-value set for sales; KRA genuinely uses two separate cd_cls numbers.
    "inventory-adjustment-reason": "35",  # cdClsNm "Reason of Inventory Adjustment"
    "bank": "36",  # cdClsNm "Bank"
    "tax-office": "45",  # cdClsNm "Tax Office"
    "locale": "48",  # cdClsNm "LOCALE"
    "category-level": "49",  # cdClsNm "Category Level" (item-classification hierarchy tiers)
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
            await _upsert_code_class(
                db, cd_cls=cls.cdCls, cd_cls_nm=cls.cdClsNm, cd_cls_desc=cls.cdClsDesc
            )
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


async def _upsert_code_class(
    db: AsyncSession, cd_cls: str, cd_cls_nm: str | None, cd_cls_desc: str | None
) -> None:
    stmt = pg_insert(KraCodeClassification).values(
        cd_cls=cd_cls, cd_cls_nm=cd_cls_nm, cd_cls_desc=cd_cls_desc
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[KraCodeClassification.cd_cls],
        set_={
            "cd_cls_nm": stmt.excluded.cd_cls_nm,
            "cd_cls_desc": stmt.excluded.cd_cls_desc,
            "refreshed_at": func.now(),
        },
    )
    await db.execute(stmt)


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


async def get_codes(
    db: AsyncSession, classification: str, q: str | None = None
) -> list[KraCodeList]:
    """Serves GET /v1/codes/{classification} from the local cache — never a KRA passthrough.
    `q` (optional) is a case-insensitive substring match against cd/cd_nm, used by the admin
    browsing endpoint (app/api/v1/endpoints/admin_reference_data.py) — the client-facing
    codes.py endpoint never passes it, so this stays a no-op there.
    """
    cd_cls = resolve_classification(classification)
    stmt = select(KraCodeList).where(KraCodeList.cd_cls == cd_cls)
    if q:
        needle = f"%{q.casefold()}%"
        stmt = stmt.where(
            func.lower(KraCodeList.cd).like(needle) | func.lower(KraCodeList.cd_nm).like(needle)
        )
    result = await db.execute(stmt.order_by(KraCodeList.srt_ord))
    return list(result.scalars().all())


REVERSE_CLASSIFICATION_SLUGS: dict[str, str] = {v: k for k, v in CLASSIFICATION_SLUGS.items()}


def canonical_classification_key(classification: str) -> str:
    """The name CustomerCodeAllowlist rows are keyed by (app/services/mapping/generic_mapper.py
    always enforces the allow-list using the friendly slug, e.g. "tax-type", never a raw
    cd_cls) — the friendly slug when one exists for this input, whether that input was already
    a slug or a raw cd_cls, otherwise the input as-is. Mirrors get_classification_summary's own
    `row.slug or row.cd_cls` fallback, just resolving in the opposite direction from
    resolve_classification (slug/raw -> cd_cls) above.
    """
    if classification in CLASSIFICATION_SLUGS:
        return classification
    return REVERSE_CLASSIFICATION_SLUGS.get(classification, classification)


class ClassificationSummary(NamedTuple):
    cd_cls: str
    slug: str | None
    cd_cls_nm: str | None
    count: int


async def get_classification_summary(db: AsyncSession) -> list[ClassificationSummary]:
    """One row per cd_cls actually present in the cache, with its friendly slug where
    CLASSIFICATION_SLUGS knows one (some real cd_cls values returned by KRA — e.g. those seen
    for insurance/branch-type tables — aren't in that spec-derived table at all; they're still
    listed here by raw cd_cls so nothing seeded is hidden from admin browsing) plus KRA's own
    real name for the classification (cd_cls_nm, from KraCodeClassification — see that model's
    docstring) wherever a sync has captured one, regardless of whether a friendly slug exists.
    """
    result = await db.execute(
        select(KraCodeList.cd_cls, func.count(), func.max(KraCodeClassification.cd_cls_nm))
        .outerjoin(KraCodeClassification, KraCodeClassification.cd_cls == KraCodeList.cd_cls)
        .group_by(KraCodeList.cd_cls)
        .order_by(KraCodeList.cd_cls)
    )
    return [
        ClassificationSummary(
            cd_cls=cd_cls,
            slug=REVERSE_CLASSIFICATION_SLUGS.get(cd_cls),
            cd_cls_nm=cd_cls_nm,
            count=count,
        )
        for cd_cls, count, cd_cls_nm in result.all()
    ]
