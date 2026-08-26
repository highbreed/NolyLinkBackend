"""Generates invcNo — spec §6.6 requires it on every /saveTrnsSalesOsdc request, but nothing
in the spec describes KRA assigning it to us, so the gateway owns a per-(tin, bhfId) sequence,
the same atomic-upsert pattern as app/services/item_code_generator.py.
"""

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SalesInvoiceSequence


async def next_invoice_no(db: AsyncSession, tin: str, bhf_id: str) -> str:
    stmt = (
        pg_insert(SalesInvoiceSequence)
        .values(tin=tin, bhf_id=bhf_id, next_seq=1)
        .on_conflict_do_update(
            index_elements=[SalesInvoiceSequence.tin, SalesInvoiceSequence.bhf_id],
            set_={"next_seq": SalesInvoiceSequence.next_seq + 1},
        )
        .returning(SalesInvoiceSequence.next_seq)
    )
    result = await db.execute(stmt)
    seq: int = result.scalar_one()
    return str(seq)
