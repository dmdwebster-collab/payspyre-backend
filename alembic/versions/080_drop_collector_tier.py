"""Remove the collector "tier" concept from collections assignment.

Revision ID: 080_drop_collector_tier
Revises: 079_loan_agreement_signed_at
Create Date: 2026-07-28

The junior/senior collector classification is withdrawn by the platform owner:
assignment stands on its own, and if a manager wants a file handled by a more
experienced collector they assign it to that person. The system does not
classify files as junior or senior work, and there is no longer a rule that
blocks a collector from a deep bucket.

This DROPS ``platform_collector_assignments.tier`` and the now-unreferenced
``platform_collector_tier`` enum type. The column is dropped rather than
retired-in-place because it is a pure classification label with no downstream
consumer: nothing joins on it, nothing aggregates it, and no money, bucket or
audit decision was ever derived from its value. The ASSIGNMENTS themselves —
who holds which loan, how it was assigned, and the full unassign history — are
untouched; only the junior/senior label is removed. The historical
``collector_assigned`` platform events keep whatever payloads they were written
with (events are immutable), so the past labels remain forensically readable
even though the live table no longer carries them.

Reversible: downgrade recreates the enum and the NOT NULL column, backfilling
existing rows with ``senior`` — the permissive value, so no restored row would
retroactively violate the gate that once existed.

Static DDL only (no interpolation — bandit B608 N/A).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "080_drop_collector_tier"
down_revision = "079_loan_agreement_signed_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("platform_collector_assignments", "tier")
    postgresql.ENUM(name="platform_collector_tier").drop(
        op.get_bind(), checkfirst=True
    )


def downgrade() -> None:
    bind = op.get_bind()
    tier = postgresql.ENUM("junior", "senior", name="platform_collector_tier")
    tier.create(bind, checkfirst=True)
    op.add_column(
        "platform_collector_assignments",
        sa.Column(
            "tier",
            postgresql.ENUM(name="platform_collector_tier", create_type=False),
            nullable=False,
            # Existing rows lost their label on upgrade; restore them as the
            # permissive tier so none of them violates the re-created gate.
            server_default="senior",
        ),
    )
    op.alter_column(
        "platform_collector_assignments", "tier", server_default=None
    )
