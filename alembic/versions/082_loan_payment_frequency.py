"""Loan booking becomes FREQUENCY-AWARE: persist the contract's repayment cadence.

Revision ID: 082_loan_payment_frequency
Revises: 081_application_number_loan_number
Create Date: 2026-07-29

THE GAP THIS CLOSES
-------------------
Payment Frequency was captured at origination, validated against the credit
product, priced by the ``/admin/origination/quote`` engine (which returns a
correct frequency-aware preliminary schedule) and printed on the loan agreement
the borrower signs — and then the loan was BOOKED MONTHLY, because
``loan_servicing.generate_amortization_schedule`` stepped in months only and
``platform_loans`` had nowhere to record anything else. A bi-weekly deal became
a monthly loan the moment it was activated.

That is also why the CEO's own validated servicing example — $10,000 / 48 months
/ 12.99% / $123.45 installment, **bi-weekly** (docs/dave_review_2026-07-21/
AMOUNT_TO_MOVE_MODEL.md) — could not be booked on the platform at all, even
though ``servicing_status`` has always supported all four frequencies.

WHAT THIS COLUMN IS
-------------------
``platform_loans.payment_frequency`` — one of the canonical
``pricing_config.PaymentFrequency`` values: ``weekly`` | ``bi_weekly`` |
``semi_monthly`` | ``monthly``. ``term_months`` remains the CONTRACT unit (that
is how the product, the offer and the agreement express a term); this column
says how many installments that term contains and how far apart they fall.

WHY A COLUMN RATHER THAN INFERRING IT FROM THE SCHEDULE
-------------------------------------------------------
``servicing_status`` currently guesses the cadence from the gap between the
first two schedule rows. That guess is fine for an untouched plan but is not
evidence: schedule surgery (WS-F) can suspend, move or re-date installments, a
one-row schedule has no gap at all, and a 14-day gap is ambiguous with
semi-monthly until a third row disambiguates it. The frequency is a CONTRACT
term, so it is stored as one. The inference stays as the fallback for legacy
rows (it is what they were serviced by until now).

BACKFILL — DELIBERATELY NOT A RETRO-RELABEL
-------------------------------------------
Every existing row defaults to ``'monthly'``. That is not an assumption, it is
the fact: the pre-082 booking engine could not emit anything else, so every
booked schedule in the database IS monthly, whatever frequency its application
or offer asked for. Relabelling those rows to their requested frequency would
make the stored cadence contradict the stored schedule and would silently
change their DPD. The divergence between what such a borrower signed and what
was booked is a remediation question for the business, not something a
migration should paper over.

Reversible: downgrade drops the column and its check constraint. Static DDL
only — no string interpolation anywhere (bandit B608 N/A).
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "082_loan_payment_frequency"
down_revision = "081_application_number_loan_number"
branch_labels = None
depends_on = None

_TABLE = "platform_loans"
_COLUMN = "payment_frequency"
_CHECK = "ck_platform_loans_payment_frequency"


def upgrade() -> None:
    # NOT NULL with a server default: existing rows are filled in one pass and
    # every insert that predates the model change keeps working.
    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN,
            sa.String(),
            nullable=False,
            server_default="monthly",
        ),
    )
    # Fail-closed on the value set: a typo ('bi-weekly', 'Monthly') must not
    # reach the servicing engine, which would then fall back to inference and
    # service the loan on a cadence nobody chose.
    op.create_check_constraint(
        _CHECK,
        _TABLE,
        sa.text(
            "payment_frequency IN ('weekly', 'bi_weekly', 'semi_monthly', 'monthly')"
        ),
    )


def downgrade() -> None:
    op.drop_constraint(_CHECK, _TABLE, type_="check")
    op.drop_column(_TABLE, _COLUMN)
