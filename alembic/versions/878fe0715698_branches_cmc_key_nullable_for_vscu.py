"""branches.cmc_key nullable for VSCU

Revision ID: 878fe0715698
Revises: a0f910864587
Create Date: 2026-09-02

Fixes a real bug surfaced by a live "kra_base_url_vscu_sandbox is not configured" error: VSCU
device registration was routing through the same real-KRA /selectInitOsdcInfo call as OSCU,
which decompiling vscu-v2.0.21.jar confirms doesn't exist for VSCU at all (its only
initialization endpoint is /initializer/selectInitInfo against the LOCAL VSCU app — see
docs/kra_specs/README.md's 2026-09-01 correction and app/services/cmc_key_manager.py's
_register_vscu_branch_locally). A VSCU Branch row is now created locally with no cmcKey (the
local VSCU app owns its own KRA trust anchor internally, never exposed to this gateway), so the
column can no longer be NOT NULL.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "878fe0715698"
down_revision: str | None = "a0f910864587"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("branches", "cmc_key", nullable=True)


def downgrade() -> None:
    # Irreversible if any VSCU branch has a NULL cmc_key (there is no value to backfill it
    # with) — this will fail loudly against real data rather than silently corrupting it.
    op.alter_column("branches", "cmc_key", nullable=False)
