"""Enforce one loan per application (unique constraint on platform_loans.application_id)

Revision ID: 032_loan_application_unique
Revises: 031_marketplace
Create Date: 2026-06-20

Audit finding: ``book_loan`` guarded against double-booking with a SELECT-then-INSERT,
but nothing enforced one-loan-per-application at the DB level — a retried/concurrent
approval (or any out-of-band caller) could create two loans for one application, i.e.
duplicate amortization schedules, duplicate Zumrails disbursements, duplicate SignNow
agreements. This replaces the non-unique ``idx_platform_loans_application`` index with a
UNIQUE constraint so the invariant is enforced by Postgres; ``book_loan`` catches the
IntegrityError and returns the existing loan (true idempotency).
"""
from typing import Sequence, Union

from alembic import op


revision: str = "032_loan_application_unique"
down_revision: Union[str, None] = "031_marketplace"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop the redundant non-unique index; the unique constraint creates its own index.
    op.drop_index("idx_platform_loans_application", table_name="platform_loans")
    op.create_unique_constraint(
        "uq_platform_loans_application", "platform_loans", ["application_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_platform_loans_application", "platform_loans", type_="unique")
    op.create_index(
        "idx_platform_loans_application", "platform_loans", ["application_id"]
    )
