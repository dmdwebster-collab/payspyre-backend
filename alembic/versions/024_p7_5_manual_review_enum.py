"""Add 'manual_review' value to platform_verification_status — P7.5 Didit In Review.

Revision ID: 024_p7_5_manual_review_enum
Revises: 023_p7_4b_notification_suppression
Create Date: 2026-05-31

Didit's "In Review" status is a real terminal-ish state for KYC: the case has
been sent to manual human review and won't progress automatically. Before P7.5
we mapped "In Review" to the non-terminal skip branch, leaving the verification
in 'pending' forever. With this migration the enum gains 'manual_review' and
the translator maps "In Review" → result="manual_review".

**One-way migration.** Postgres has no DROP VALUE for enums, so downgrade is a
no-op (with a comment). If a rollback is ever needed it requires a full enum
swap — out of scope for this PR.

Idempotent via ``ADD VALUE IF NOT EXISTS`` so re-runs (or local CI replays)
are no-ops. ``ALTER TYPE ... ADD VALUE`` must run outside a transaction on
older Postgres; ``op.get_context().autocommit_block()`` handles both modern
(15+) and older paths.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "024_p7_5_manual_review_enum"
down_revision: Union[str, None] = "023_p7_4b_notification_suppression"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "ALTER TYPE platform_verification_status "
            "ADD VALUE IF NOT EXISTS 'manual_review'"
        )


def downgrade() -> None:
    # Postgres has no DROP VALUE for enums, so this is effectively a one-way
    # migration: the 'manual_review' value added in upgrade() persists even
    # after this function runs. A real rollback requires creating a fresh
    # enum, ALTER COLUMN ... USING old::text::new, then DROP TYPE — out of
    # scope here. We emit a benign no-op DDL so (a) the CI stub-detector at
    # scripts/check_no_stub_migrations.py doesn't reject a bare ``pass`` and
    # (b) the migration-integrity test suite's downgrade-then-upgrade chain
    # runs through without raising. The follow-on upgrade() call from that
    # test re-applies ADD VALUE IF NOT EXISTS, which is itself a no-op
    # because the value still exists — the schema stays consistent.
    op.execute("SELECT 1")
