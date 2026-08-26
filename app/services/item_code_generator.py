"""KRA-format itemCd auto-generator (spec §8).

Format: KE2NTBA0000012 = origin nation code + product type code + packaging unit code +
quantity unit code + a 7-digit zero-padded sequence starting at 0000001, scoped per
(tin, bhfId, orgnNatCd, itemTyCd, pkgUnitCd, qtyUnitCd). Each segment is used at its natural
length (e.g. "2" for product type, not padded) — only the trailing sequence is fixed-width.
"""

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ItemCodeSequence


async def _next_seq(
    db: AsyncSession,
    tin: str,
    bhf_id: str,
    orgn_nat_cd: str,
    item_ty_cd: str,
    pkg_unit_cd: str,
    qty_unit_cd: str,
) -> int:
    stmt = (
        pg_insert(ItemCodeSequence)
        .values(
            tin=tin,
            bhf_id=bhf_id,
            orgn_nat_cd=orgn_nat_cd,
            item_ty_cd=item_ty_cd,
            pkg_unit_cd=pkg_unit_cd,
            qty_unit_cd=qty_unit_cd,
            next_seq=1,
        )
        .on_conflict_do_update(
            index_elements=[
                ItemCodeSequence.tin,
                ItemCodeSequence.bhf_id,
                ItemCodeSequence.orgn_nat_cd,
                ItemCodeSequence.item_ty_cd,
                ItemCodeSequence.pkg_unit_cd,
                ItemCodeSequence.qty_unit_cd,
            ],
            set_={"next_seq": ItemCodeSequence.next_seq + 1},
        )
        .returning(ItemCodeSequence.next_seq)
    )
    result = await db.execute(stmt)
    next_seq: int = result.scalar_one()
    return next_seq


async def generate_item_cd(
    db: AsyncSession,
    *,
    tin: str,
    bhf_id: str,
    orgn_nat_cd: str,
    item_ty_cd: str,
    pkg_unit_cd: str,
    qty_unit_cd: str,
) -> str:
    seq = await _next_seq(db, tin, bhf_id, orgn_nat_cd, item_ty_cd, pkg_unit_cd, qty_unit_cd)
    return f"{orgn_nat_cd}{item_ty_cd}{pkg_unit_cd}{qty_unit_cd}{seq:07d}"
