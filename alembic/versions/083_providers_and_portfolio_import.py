"""Providers as a first-class entity + a source-neutral portfolio-import provenance.

Revision ID: 083_providers_and_portfolio_import
Revises: 082_loan_payment_frequency
Create Date: 2026-07-29

TWO CHANGES, ONE THEME: PaySpyre imports loan portfolios REGARDLESS OF SOURCE.

1. PROVIDERS BELONG TO THE VENDOR
   ------------------------------
   "Provider" was free text on the application (``provider_name``) and the
   Originations dropdown was built by SELECT DISTINCT over application history —
   so the option list was a by-product of past typing: misspellings became
   permanent options, a brand-new vendor had an empty dropdown, and a departed
   practitioner could never be retired. This creates ``platform_providers``,
   owned by ``vendors``, and links applications and loans to it.

   ``provider_name`` is NOT dropped. It holds history that no table can
   reconstruct, and callers still read it; the importer and the origination
   endpoints now write BOTH (the id for the relationship, the name for the
   record).

2. ``platform_loans.source`` GAINS A SOURCE-NEUTRAL VALUE
   ------------------------------------------------------
   Migration 035 allowed a NULL ``application_id`` only when
   ``source = 'turnkey_migration'`` — the name of the first legacy system the
   platform migrated off, baked into a CHECK constraint. Future vendors arrive
   with books from entirely different servicing systems, so the constraint is
   widened to accept the neutral ``'portfolio_import'`` as well.

   The legacy value is DELIBERATELY RETAINED rather than rewritten: it is the
   true provenance of the rows that carry it, and re-labelling live rows would
   destroy that fact for no benefit. New imports write ``'portfolio_import'``;
   readers accept both (``app/services/migration/constants.py``).

Reversible. Downgrade drops the new table/columns and restores the original
single-value CHECK — which is only safe while no row carries the new value, so
the downgrade refuses if any does. Static DDL only (bandit B608 N/A).
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "083_providers_and_portfolio_import"
down_revision = "082_loan_payment_frequency"
branch_labels = None
depends_on = None

_LOAN_SOURCE_CHECK = "ck_platform_loans_application_or_migration"
_CI_INDEX = "uq_platform_providers_vendor_name_ci"


def upgrade() -> None:
    # --- 1. platform_providers --------------------------------------------
    op.create_table(
        "platform_providers",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "vendor_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vendors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("external_code", sa.String(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("source", sa.String(), nullable=False, server_default="manual"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("vendor_id", "name", name="uq_platform_providers_vendor_name"),
    )
    op.create_index("ix_platform_providers_vendor", "platform_providers", ["vendor_id"])
    # Case-insensitive roster uniqueness: "Dr. Jane Roe" and "DR. JANE ROE" are
    # the same person. Functional index -> DB-only artefact (no model expression).
    op.execute(
        f"CREATE UNIQUE INDEX {_CI_INDEX} "
        "ON platform_providers (vendor_id, lower(name))"
    )

    # --- 2. provider links -------------------------------------------------
    op.add_column(
        "platform_credit_applications",
        sa.Column(
            "provider_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_providers.id"),
            nullable=True,
        ),
    )
    op.add_column(
        "platform_loans",
        sa.Column(
            "provider_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("platform_providers.id"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_platform_credit_applications_provider",
        "platform_credit_applications",
        ["provider_id"],
    )
    op.create_index("ix_platform_loans_provider", "platform_loans", ["provider_id"])

    # --- 3. vendor + borrower links the importer needs ---------------------
    # A migrated loan knows which vendor's book it came from. Natively-originated
    # loans reach their vendor through the application; imported loans have none.
    op.add_column(
        "platform_loans",
        sa.Column(
            "vendor_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vendors.id"),
            nullable=True,
        ),
    )
    op.create_index("ix_platform_loans_vendor", "platform_loans", ["vendor_id"])

    # The source system's own vendor identifier (e.g. "BC4906"). Without it an
    # import can only match a vendor by business name, which is neither stable
    # nor unique. Unique when present so a re-import resolves to the same vendor.
    op.add_column("vendors", sa.Column("external_code", sa.String(), nullable=True))
    op.create_index(
        "uq_vendors_external_code", "vendors", ["external_code"], unique=True
    )

    # --- 4. widen the migrated-loan provenance CHECK -----------------------
    op.drop_constraint(_LOAN_SOURCE_CHECK, "platform_loans", type_="check")
    op.create_check_constraint(
        _LOAN_SOURCE_CHECK,
        "platform_loans",
        "source IN ('turnkey_migration', 'portfolio_import') OR application_id IS NOT NULL",
    )


def downgrade() -> None:
    # Restoring the single-value CHECK is only safe if nothing relies on the new
    # value. Refuse loudly rather than leave rows that violate the constraint.
    bind = op.get_bind()
    offending = bind.execute(
        sa.text(
            "SELECT count(*) FROM platform_loans "
            "WHERE source = 'portfolio_import' AND application_id IS NULL"
        )
    ).scalar()
    if offending:
        raise RuntimeError(
            f"{offending} loan(s) carry source='portfolio_import' with no application; "
            "downgrading would violate the restored CHECK constraint. Re-label or "
            "remove those rows first."
        )

    op.drop_constraint(_LOAN_SOURCE_CHECK, "platform_loans", type_="check")
    op.create_check_constraint(
        _LOAN_SOURCE_CHECK,
        "platform_loans",
        "source = 'turnkey_migration' OR application_id IS NOT NULL",
    )

    op.drop_index("uq_vendors_external_code", table_name="vendors")
    op.drop_column("vendors", "external_code")
    op.drop_index("ix_platform_loans_vendor", table_name="platform_loans")
    op.drop_column("platform_loans", "vendor_id")
    op.drop_index("ix_platform_loans_provider", table_name="platform_loans")
    op.drop_index(
        "ix_platform_credit_applications_provider",
        table_name="platform_credit_applications",
    )
    op.drop_column("platform_loans", "provider_id")
    op.drop_column("platform_credit_applications", "provider_id")

    op.execute(f"DROP INDEX IF EXISTS {_CI_INDEX}")
    op.drop_index("ix_platform_providers_vendor", table_name="platform_providers")
    op.drop_table("platform_providers")
