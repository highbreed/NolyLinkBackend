"""kra code classification names

Revision ID: e2b6f7c1a4d3
Revises: d1f4a6b2c9e0
Create Date: 2026-09-18

New, empty table — no backfill needed. KRA's /selectCodeList (and VSCU's /code/selectCodes)
response carries a real name (cdClsNm) for each cd_cls alongside its codes; earlier syncs
discarded it. This table captures it going forward; existing kra_code_lists rows are
unaffected, and a re-sync backfills kra_code_classifications for any branch already synced.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e2b6f7c1a4d3"
down_revision: str | None = "d1f4a6b2c9e0"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "kra_code_classifications",
        sa.Column("cd_cls", sa.String(), nullable=False),
        sa.Column("cd_cls_nm", sa.String(), nullable=True),
        sa.Column("cd_cls_desc", sa.String(), nullable=True),
        sa.Column(
            "refreshed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("cd_cls"),
    )


def downgrade() -> None:
    op.drop_table("kra_code_classifications")
