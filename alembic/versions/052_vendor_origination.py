"""Vendor origination — structured intake fields + reprocessing flag (WS-I)

Revision ID: 052_vendor_origination
Revises: 049_loan_ledger
Create Date: 2026-07-10

Turnkey parity WS-I (docs/turnkey_parity/10__Vendor_Access.md — Dave's vendor
portal spec): vendors originate applications on the patient's behalf via the
clinic surface, carrying the "Application Submission Checklist" money model
(treatment cost − insurance − down payment = amount financed), the patient's
stated payment preferences, and the vendor-arranged term/rate/dates.

All columns are additive + nullable (existing rows and the patient-originated
path are untouched) except ``vendor_reprocessing_requested`` which defaults to
false. Schema is kept minimal (build-plan rule: only what queries need):

* Checklist money model (integer cents): ``treatment_cost_cents``,
  ``insurance_coverage_cents``, ``down_payment_cents``. The financed amount
  itself lands in the existing ``requested_amount_cents`` (invariant enforced
  at the intake endpoint: cost − insurance − down = financed).
* Patient payment preferences (checklist rows 11–13):
  ``preferred_payment_amount_cents``, ``preferred_payment_frequency``,
  ``preferred_first_due_date``.
* Vendor-arranged terms: ``requested_term_months``,
  ``requested_annual_rate_bps`` (role-gated within the product's
  PricingConfig rate band), ``loan_start_date``, ``first_due_date`` (the
  "custom first due date" from the Turnkey new-application form).
* ``provider_name`` — free-text provider (the treating dentist). There is no
  providers directory table yet (the widget intake also carries provider as a
  display string); a structured Vendor→Provider directory is FLAGGED FOR DAVE.
* ``vendor_reprocessing_requested`` — set by the vendor's single allowed
  underwriting action (POST …/request-reprocessing); the admin queue filters
  on it, hence the partial index. Cleared when staff record a decision.

Checklist rows 14–16 (additional offers / alt-contact / notes) are unqueried
free text and live in ``self_reported['vendor_intake']`` — no columns.

The reprocessing request itself is an append-only ``platform_events`` row
(``vendor_reprocessing_requested``) — no event table changes needed.

NOTE ON CHAINING: chained from 049_loan_ledger (the ledger migration landed on
main before this branch pushed, per the WS-I instruction), giving a single
linear head at 052.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "052_vendor_origination"
down_revision: Union[str, None] = "051_auto_collection"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "platform_credit_applications"


def upgrade() -> None:
    # --- checklist money model (integer cents) ------------------------------
    op.add_column(_TABLE, sa.Column("treatment_cost_cents", sa.BigInteger(), nullable=True))
    op.add_column(_TABLE, sa.Column("insurance_coverage_cents", sa.BigInteger(), nullable=True))
    op.add_column(_TABLE, sa.Column("down_payment_cents", sa.BigInteger(), nullable=True))

    # --- patient payment preferences (checklist rows 11-13) -----------------
    op.add_column(
        _TABLE, sa.Column("preferred_payment_amount_cents", sa.BigInteger(), nullable=True)
    )
    op.add_column(_TABLE, sa.Column("preferred_payment_frequency", sa.String(), nullable=True))
    op.add_column(_TABLE, sa.Column("preferred_first_due_date", sa.Date(), nullable=True))

    # --- vendor-arranged terms ----------------------------------------------
    op.add_column(_TABLE, sa.Column("requested_term_months", sa.Integer(), nullable=True))
    op.add_column(_TABLE, sa.Column("requested_annual_rate_bps", sa.Integer(), nullable=True))
    op.add_column(_TABLE, sa.Column("provider_name", sa.String(), nullable=True))
    op.add_column(_TABLE, sa.Column("loan_start_date", sa.Date(), nullable=True))
    op.add_column(_TABLE, sa.Column("first_due_date", sa.Date(), nullable=True))

    # --- vendor "request reprocessing" flag (admin-queue filterable) --------
    op.add_column(
        _TABLE,
        sa.Column(
            "vendor_reprocessing_requested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # Partial index: the admin queue filters WHERE vendor_reprocessing_requested,
    # and true rows are a tiny slice of the book.
    op.create_index(
        "ix_platform_credit_applications_vendor_reproc",
        _TABLE,
        ["vendor_reprocessing_requested"],
        postgresql_where=sa.text("vendor_reprocessing_requested"),
    )


def downgrade() -> None:
    op.drop_index("ix_platform_credit_applications_vendor_reproc", _TABLE)
    op.drop_column(_TABLE, "vendor_reprocessing_requested")
    op.drop_column(_TABLE, "first_due_date")
    op.drop_column(_TABLE, "loan_start_date")
    op.drop_column(_TABLE, "provider_name")
    op.drop_column(_TABLE, "requested_annual_rate_bps")
    op.drop_column(_TABLE, "requested_term_months")
    op.drop_column(_TABLE, "preferred_first_due_date")
    op.drop_column(_TABLE, "preferred_payment_frequency")
    op.drop_column(_TABLE, "preferred_payment_amount_cents")
    op.drop_column(_TABLE, "down_payment_cents")
    op.drop_column(_TABLE, "insurance_coverage_cents")
    op.drop_column(_TABLE, "treatment_cost_cents")
