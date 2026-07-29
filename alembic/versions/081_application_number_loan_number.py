"""The Application Number becomes the Loan ID (owner instruction, 2026-07-28).

Revision ID: 081_application_number_loan_number
Revises: 080_drop_collector_tier
Create Date: 2026-07-28

Dave: *"A loan can, and must, be fully set up, approved, accepted, and signed
before it can be activated. The solution here is straightforward: the Application
Number becomes the Loan ID. This allows the Loan ID to be populated on the loan
agreement before activation."*

Under the activation rework (migration 078 / Wave 6) NO loan row exists until
activation, so the agreement the borrower signs rendered ``[NOT AVAILABLE:
LoanId]`` — the document was signed without the identifier it is about. The fix
is an identifier minted on the APPLICATION, which the loan then inherits.

WHY A NEW COLUMN RATHER THAN REUSING THE UUIDs
----------------------------------------------
``platform_loans.id`` cannot be set to the application's id: both are primary
keys of different tables, and making them equal would let any id mix-up silently
"work" across the money path. Nor is a raw UUID an "Application NUMBER" — it is
not a number, and it is not something a borrower can read off a signed
agreement. So this adds:

  * ``platform_credit_applications.application_number`` — a short, sortable,
    human-facing decimal string ("100001", "100002", ...) drawn from a dedicated
    sequence. A **column DEFAULT** mints it, so EVERY create path (applicant
    journey, vendor origination, back-office profile origination, widget intake,
    imports, fixtures) gets one with no code change and none can forget.
  * ``platform_loans.loan_number`` — the SAME string, copied from the
    application when the loan is booked. The signed agreement and the live loan
    therefore carry one identifier, which is the whole point of the instruction.

Migrated Turnkey loans (``application_id IS NULL``) keep ``loan_number`` NULL and
go on being identified by ``legacy_account_number`` — vendors keep the numbers
they already know.

Backfill: existing applications are numbered in ``created_at`` order (oldest =
lowest), then the sequence is advanced past the highest number issued so new
rows can never collide. Existing loans inherit their application's number.

Reversible: downgrade drops both columns, both unique indexes and the sequence.
Static DDL/DML only — no string interpolation anywhere (bandit B608 N/A).
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "081_application_number_loan_number"
down_revision = "080_drop_collector_tier"
branch_labels = None
depends_on = None

#: Numbers start here so an application number is never confusably short and
#: never collides with a small legacy Turnkey account number.
_SEQUENCE_START = 100_000


def upgrade() -> None:
    # --- applications: the number itself ----------------------------------
    op.add_column(
        "platform_credit_applications",
        sa.Column("application_number", sa.String(), nullable=True),
    )

    # Backfill oldest-first so the numbering reads chronologically. row_number()
    # over a stable (created_at, id) ordering keeps this deterministic and
    # re-runnable on any copy of the database.
    op.execute(
        sa.text(
            """
            WITH ordered AS (
                SELECT id,
                       row_number() OVER (ORDER BY created_at, id) AS rn
                  FROM platform_credit_applications
            )
            UPDATE platform_credit_applications AS a
               SET application_number = (:start + ordered.rn)::text
              FROM ordered
             WHERE a.id = ordered.id
            """
        ).bindparams(start=_SEQUENCE_START)
    )

    op.create_index(
        "uq_platform_credit_applications_number",
        "platform_credit_applications",
        ["application_number"],
        unique=True,
    )

    # The sequence, advanced past everything the backfill issued. setval's third
    # argument false => the NEXT nextval() returns exactly this value.
    op.execute(sa.text("CREATE SEQUENCE platform_application_number_seq"))
    op.execute(
        sa.text(
            """
            SELECT setval(
                'platform_application_number_seq',
                GREATEST(
                    :start,
                    (SELECT COALESCE(MAX(application_number::bigint), 0)
                       FROM platform_credit_applications)
                ) + 1,
                false
            )
            """
        ).bindparams(start=_SEQUENCE_START)
    )
    # The DEFAULT is what makes this unmissable: no INSERT path can omit it.
    op.execute(
        sa.text(
            "ALTER TABLE platform_credit_applications "
            "ALTER COLUMN application_number "
            "SET DEFAULT nextval('platform_application_number_seq')::text"
        )
    )
    op.alter_column(
        "platform_credit_applications", "application_number", nullable=False
    )
    # The sequence belongs to the column: DROP COLUMN takes it with it, and
    # pg_dump orders them correctly.
    op.execute(
        sa.text(
            "ALTER SEQUENCE platform_application_number_seq "
            "OWNED BY platform_credit_applications.application_number"
        )
    )

    # --- loans: the inherited copy ----------------------------------------
    # Nullable: migrated Turnkey loans have no application to inherit from.
    op.add_column(
        "platform_loans", sa.Column("loan_number", sa.String(), nullable=True)
    )
    op.execute(
        sa.text(
            """
            UPDATE platform_loans AS l
               SET loan_number = a.application_number
              FROM platform_credit_applications AS a
             WHERE l.application_id = a.id
            """
        )
    )
    op.create_index(
        "uq_platform_loans_number",
        "platform_loans",
        ["loan_number"],
        unique=True,
        postgresql_where=sa.text("loan_number IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_platform_loans_number", table_name="platform_loans")
    op.drop_column("platform_loans", "loan_number")
    op.drop_index(
        "uq_platform_credit_applications_number",
        table_name="platform_credit_applications",
    )
    # The column owns the sequence, so dropping it drops the sequence too.
    op.drop_column("platform_credit_applications", "application_number")
