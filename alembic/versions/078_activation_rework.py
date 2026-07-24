"""Activation-rework Wave 1: pre-loan agreement/signature state on the application.

Revision ID: 078_activation_rework
Revises: 077_integration_mode
Create Date: 2026-07-24

Groundwork for the "loan created at Activation, not Approval" rework. This wave
is ADDITIVE ONLY — it changes no current behaviour. The behavioural cutover
(booking the loan later, at activation, instead of at approval) is a LATER wave;
nothing here alters ``book_loan`` or any approve/accept path.

ADDS to ``platform_credit_applications`` (an agreement can now be sent + signed
BEFORE a loan exists — today that state lives only on ``platform_loans``):

    agreement_status     platform_loan_agreement_status  NOT NULL default 'not_sent'
    agreement_ref        text  NULL   (SignNow document id / SIMULATED-<id> marker)
    agreement_signed_at  timestamptz  NULL

The ENUM reuses the EXISTING ``platform_loan_agreement_status`` PG type (created
in migration 026 for ``PlatformLoan.agreement_status``): not_sent / sent /
signed / declined. It is declared WITH its full value list and
``create_type=False`` so Alembic references the existing type instead of trying
to re-CREATE it, and so the column read-maps correctly (an ENUM declared without
its values causes read-mapping 500s).

ADDS to ``platform_loans``:

    booked_at_activation boolean  NOT NULL default false

Grandfathering flag for the cutover: every loan booked under the old approve-time
path is ``false`` (the server_default backfills existing rows); loans booked at
activation under the future path will set it ``true``. Inert this wave — nothing
reads it yet — but it lets the later cutover distinguish the two cohorts without
a second migration on the money table.

Reversible: ``downgrade`` drops exactly the four added columns and leaves the
shared ENUM type in place (``platform_loans`` still uses it). Static DDL only
(no interpolation — bandit B608 N/A).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "078_activation_rework"
down_revision = "077_integration_mode"
branch_labels = None
depends_on = None


# The EXISTING enum type shared with PlatformLoan.agreement_status. Declared with
# its full value list + create_type=False so we reference (never re-create) it.
_AGREEMENT_STATUS = postgresql.ENUM(
    "not_sent",
    "sent",
    "signed",
    "declined",
    name="platform_loan_agreement_status",
    create_type=False,
)


def upgrade() -> None:
    # 1. Pre-loan agreement/signature state on the application.
    op.add_column(
        "platform_credit_applications",
        sa.Column(
            "agreement_status",
            _AGREEMENT_STATUS,
            nullable=False,
            server_default="not_sent",
        ),
    )
    op.add_column(
        "platform_credit_applications",
        sa.Column("agreement_ref", sa.String(), nullable=True),
    )
    op.add_column(
        "platform_credit_applications",
        sa.Column("agreement_signed_at", sa.DateTime(timezone=True), nullable=True),
    )

    # 2. Grandfathering flag on the loan (existing rows backfill to false).
    op.add_column(
        "platform_loans",
        sa.Column(
            "booked_at_activation",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("platform_loans", "booked_at_activation")
    op.drop_column("platform_credit_applications", "agreement_signed_at")
    op.drop_column("platform_credit_applications", "agreement_ref")
    op.drop_column("platform_credit_applications", "agreement_status")
