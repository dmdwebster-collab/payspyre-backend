"""Rename credit-decision status ``declined`` -> ``rejected`` (Dave 2026-07-22).

Revision ID: 076_rejected_status
Revises: 075_write_off_permission
Create Date: 2026-07-23

Dave's directive (DAVE_DIRECTIVES_2026-07-22.md §5, stated twice): the terminal
credit-decision status is **Rejected**, NOT "Declined". "Rejected" and
"Cancelled" are independent statuses, each with its own reason list — the reason
directory (migration 048, ``platform_decision_reasons``) already scopes reject
vs cancel reasons separately, so this migration only renames the STATUS value.

``ALTER TYPE ... RENAME VALUE`` renames the enum label in place: every existing
``platform_credit_applications.status = 'declined'`` row reads ``'rejected'``
immediately afterwards (same enum OID, no row rewrite, no data-loss window). It
is transaction-safe on PostgreSQL 10+, so no autocommit block is needed (unlike
``ADD VALUE``).

SCOPE — this touches ONLY ``platform_application_status``. The internal
credit-decision OUTCOME token ``declined`` (the scoring engine's decision
vocabulary, the risk-score override machinery, and the stored decision JSON) is
deliberately left unchanged — see the PR description. Separate enums that also
carry a ``declined`` value (marketplace ``lead_state``, e-sign ``agreement_status``,
hardship, Didit KYC) are unrelated concepts and are NOT touched.
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = "076_rejected_status"
down_revision = "075_write_off_permission"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TYPE platform_application_status RENAME VALUE 'declined' TO 'rejected'"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TYPE platform_application_status RENAME VALUE 'rejected' TO 'declined'"
    )
