"""Activation-rework Wave 2: loan-level agreement_signed_at (provenance copy).

Revision ID: 079_loan_agreement_signed_at
Revises: 078_activation_rework
Create Date: 2026-07-24

Wave 2 books the loan at ACTIVATION off a signed APPLICATION agreement. When the
loan is created, the activation executor (``loan_lifecycle.activate_loan``)
copies the agreement provenance forward from the application onto the loan so the
booked loan carries the same signing record it was created from:

    application.agreement_ref       -> loan.agreement_ref        (column existed)
    application.agreement_signed_at -> loan.agreement_signed_at   (ADDED HERE)

The loan already had ``agreement_ref`` + ``agreement_status`` (migration 026) but
never a signed-at timestamp — the old loan-level flow only flipped
``agreement_status`` on the SignNow webhook and never stamped WHEN. This adds a
nullable ``agreement_signed_at`` so the activation-booked loan can record its
signing time; grandfathered loans (booked under the old approve-time path, and
loans still awaiting signature) keep NULL.

ADDITIVE + reversible: adds one nullable ``timestamptz`` column and drops exactly
it on downgrade. No data backfill, no behaviour change on its own — inert until
the ``ACTIVATION_BOOKS_LOAN`` flag is flipped on. Static DDL only (no
interpolation — bandit B608 N/A).
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "079_loan_agreement_signed_at"
down_revision = "078_activation_rework"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "platform_loans",
        sa.Column("agreement_signed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("platform_loans", "agreement_signed_at")
