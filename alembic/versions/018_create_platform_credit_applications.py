"""Create platform_credit_applications table

Revision ID: 018_create_platform_credit_applications
Revises: 017_create_platform_credit_products
Create Date: 2026-05-21

Part of PR P1: Platform Foundation Migrations

This migration creates the platform_credit_applications table, which stores
a patient's specific application to a credit product. This replaces the v1
loan_applications table for the v2 platform layer.

Key Features:
- Patient and credit product references with version snapshot
- Co-applicant linkage via co_applicant_of_application_id (self-referential FK)
- Requested amount source tagging (clinic, patient, or clinic_then_patient_adjusted)
- Flow state JSONB tracking which verification steps have completed
- Decision JSONB with outcome (approved/declined, amount, APR, term, reason codes)
- Self-reported overrides kept separate from verified fields

Security Notes:
- Product version snapshot prevents mid-application product changes
- Flow state only updated via flow engine (enforced at service layer)
- Decision signed with decision_by (auto or manual:<operator_id>)

Hard Rules:
- State transitions ONLY via flow engine
- CI grep test forbids direct status writes outside flow_engine/
- Co-applicant is full application row; decisioning at application group level
- At most one co-applicant per primary (enforced by partial unique index in PR P3.5)

Open Questions Referenced:
- Q1 (existing applications table): create new, don't extend (ADR 0001)
- Q2 (clinic/practice table): vendor_id references existing vendors.id (ADR 0001)
- Q7 (requested_amount source): clinic and/or patient can contribute (spec §2.4.1)
- Q10 (co-applicant combination_strategy): default average (spec §4.5)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '018_create_platform_credit_applications'
down_revision: Union[str, None] = '017_create_platform_credit_products'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ===========================================================================
    # CREATE NEW ENUM TYPES
    # ===========================================================================

    # platform_credit_applications.status enum
    op.execute("""
        CREATE TYPE platform_application_status AS ENUM (
            'started',
            'verifying',
            'pre_qualified',
            'awaiting_hard_pull',
            'under_review',
            'approved',
            'declined',
            'withdrawn',
            'expired'
        )
    """)

    # platform_credit_applications.requested_amount_source enum
    op.execute("""
        CREATE TYPE platform_amount_source AS ENUM (
            'clinic',
            'patient',
            'clinic_then_patient_adjusted'
        )
    """)

    # platform_credit_applications.applicant_role enum
    op.execute("""
        CREATE TYPE platform_applicant_role AS ENUM (
            'primary',
            'co_applicant'
        )
    """)

    # Create enum types for use in columns
    status_enum = postgresql.ENUM(name='platform_application_status', create_type=False)
    amount_source_enum = postgresql.ENUM(name='platform_amount_source', create_type=False)
    applicant_role_enum = postgresql.ENUM(name='platform_applicant_role', create_type=False)

    # ===========================================================================
    # CREATE platform_credit_applications TABLE
    # ===========================================================================

    op.create_table(
        'platform_credit_applications',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('patient_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('platform_patients.id'), nullable=False),
        sa.Column('credit_product_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('platform_credit_products.id'), nullable=False),
        sa.Column('credit_product_version', sa.Integer(), nullable=False),
        # snapshot of which product version they applied under

        # Co-applicant linkage (see spec §2.11 and §4.5)
        # If non-null, this row is a co-applicant attached to the primary application.
        # The primary application's id has this column = NULL.
        sa.Column('co_applicant_of_application_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('applicant_role', applicant_role_enum, nullable=False, server_default='primary'),

        # Requested amount: source tagging — see spec §2.4.1
        sa.Column('requested_amount_cents', sa.BigInteger(), nullable=False),
        sa.Column('requested_amount_source', amount_source_enum, nullable=False),
        sa.Column('clinic_proposed_amount_cents', sa.BigInteger(), nullable=True),
        # captured if clinic seeded the amount
        sa.Column('patient_proposed_amount_cents', sa.BigInteger(), nullable=True),
        # captured if patient entered/adjusted

        # Origination context
        sa.Column('vendor_id', postgresql.UUID(as_uuid=True), nullable=True),
        # nullable for direct-to-consumer; references vendors.id
        sa.Column('treatment_plan_ref', sa.String(), nullable=True),

        # State
        sa.Column('status', status_enum, nullable=False, server_default='started'),
        sa.Column('status_updated_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),

        # Flow state — which steps have run
        sa.Column('flow_state', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        # {steps_completed: ['id', 'soft_pull'], current_step: 'bank',
        #  step_results: {id: 'pass', soft_pull: 'pre_qualified'}}

        # Outcome
        sa.Column('decision', postgresql.JSONB(), nullable=True),
        # {result: 'approved', amount_cents: 250000, apr_bps: 1299, term_months: 24, reason_codes: [...]}
        sa.Column('decision_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('decision_by', sa.String(), nullable=True),
        # 'auto' | 'manual:<operator_id>'

        # Self-reported overrides (kept separate from verified fields)
        sa.Column('self_reported', postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),

        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
    )

    # ===========================================================================
    # CREATE INDEXES
    # ===========================================================================

    # Patient lookup
    op.create_index('idx_platform_credit_applications_patient', 'platform_credit_applications', ['patient_id'])

    # Status filtering
    op.create_index('idx_platform_credit_applications_status', 'platform_credit_applications', ['status'])

    # Co-applicant lookup (partial index for performance)
    op.create_index(
        'idx_platform_credit_applications_coapp',
        'platform_credit_applications',
        ['co_applicant_of_application_id'],
        postgresql_where=sa.text('co_applicant_of_application_id IS NOT NULL')
    )

    # ===========================================================================
    # CREATE FOREIGN KEYS
    # ===========================================================================

    # Self-referential FK for co-applicant linkage
    op.create_foreign_key(
        'fk_platform_credit_applications_co_applicant_of',
        'platform_credit_applications', 'platform_credit_applications',
        ['co_applicant_of_application_id'], ['id']
    )

    # FK to vendors table (nullable for direct-to-consumer)
    op.create_foreign_key(
        'fk_platform_credit_applications_vendor',
        'platform_credit_applications', 'vendors',
        ['vendor_id'], ['id']
    )

    # ===========================================================================
    # CREATE TRIGGER FOR UPDATED_AT
    # ===========================================================================

    op.execute("""
        CREATE OR REPLACE FUNCTION update_platform_credit_applications_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)

    op.execute("""
        CREATE TRIGGER platform_credit_applications_updated_at
        BEFORE UPDATE ON platform_credit_applications
        FOR EACH ROW
        EXECUTE FUNCTION update_platform_credit_applications_updated_at();
    """)


def downgrade() -> None:
    # Drop trigger and function
    op.execute("DROP TRIGGER IF EXISTS platform_credit_applications_updated_at ON platform_credit_applications")
    op.execute("DROP FUNCTION IF EXISTS update_platform_credit_applications_updated_at()")

    # Drop foreign keys
    op.drop_constraint('fk_platform_credit_applications_vendor', 'platform_credit_applications', type_='foreignkey')
    op.drop_constraint('fk_platform_credit_applications_co_applicant_of', 'platform_credit_applications', type_='foreignkey')

    # Drop indexes
    op.drop_index('idx_platform_credit_applications_coapp', table_name='platform_credit_applications')
    op.drop_index('idx_platform_credit_applications_status', table_name='platform_credit_applications')
    op.drop_index('idx_platform_credit_applications_patient', table_name='platform_credit_applications')

    # Drop table
    op.drop_table('platform_credit_applications')

    # Drop enum types
    op.execute("DROP TYPE IF EXISTS platform_applicant_role")
    op.execute("DROP TYPE IF EXISTS platform_amount_source")
    op.execute("DROP TYPE IF EXISTS platform_application_status")
