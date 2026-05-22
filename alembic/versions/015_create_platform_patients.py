"""Create platform_patients table

Revision ID: 015_create_platform_patients
Revises: 013_add_funding_tables
Create Date: 2026-05-21

Part of PR P1: Platform Foundation Migrations

This migration creates the platform_patients table, which is the canonical
human record in PaySpyre v2. One row per person, ever.

Key Features:
- Patient profile with identity fields (legal name, DOB, email, phone)
- SIN encrypted at rest (pgcrypto), with sin_last3 for ops display
- Verification depth denormalization for fast filtering
- Lead state denormalization for marketplace queries
- Marketing and monetization flags
- Soft-delete only (PIPEDA retention obligations)
- Unique constraints on email (lowercase) and phone for active records

Security Notes:
- sin_encrypted column stores SIN with pgcrypto encryption
- sin_last3 available for ops display/search, full SIN never returned by default APIs
- SIN legally optional; declining is allowed but flagged for decisioning
- deleted_at implements soft-delete only

Hard Rules:
- Never UPDATE legal_first_name, legal_last_name, dob once verified
- New verified values go to platform_patient_fields with source tagging
- Denorm columns updated through service function that records change in platform_events

Open Questions Referenced:
- Q3 (SIN encryption): pgcrypto with PAYSPYRE_SIN_ENCRYPTION_KEY env var (ADR 0001)
- Q5 (common-name misidentification): sin_declined flagged for downstream decisioning
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '015_create_platform_patients'
down_revision: Union[str, None] = '013_add_funding_tables'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ===========================================================================
    # CREATE NEW ENUM TYPES
    # ===========================================================================

    # platform_patients.verification_depth enum
    op.execute("""
        CREATE TYPE platform_verification_depth AS ENUM (
            'none',
            'email_verified',
            'phone_verified',
            'id_verified',
            'id_bank_verified',
            'id_bank_cb_verified'
        )
    """)

    # platform_patients.lead_state enum
    op.execute("""
        CREATE TYPE platform_lead_state AS ENUM (
            'unqualified',
            'pre_qualified',
            'pre_approved',
            'approved',
            'declined'
        )
    """)

    # Create enum types for use in columns
    verification_depth_enum = postgresql.ENUM(name='platform_verification_depth', create_type=False)
    lead_state_enum = postgresql.ENUM(name='platform_lead_state', create_type=False)

    # ===========================================================================
    # CREATE platform_patients TABLE
    # ===========================================================================

    op.create_table(
        'platform_patients',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),

        # Identity (canonical, verified where possible — source-tagged in platform_patient_fields)
        sa.Column('legal_first_name', sa.String(), nullable=True),
        sa.Column('legal_last_name', sa.String(), nullable=True),
        sa.Column('dob', sa.Date(), nullable=True),
        sa.Column('email', sa.String(), nullable=True),
        sa.Column('phone_e164', sa.String(), nullable=True),

        # SIN — encrypted at rest, never logged, never returned by default APIs
        # Legally optional; declining is allowed but flagged for ops/decisioning
        sa.Column('sin_encrypted', sa.LargeBinary(), nullable=True),
        sa.Column('sin_last3', sa.String(3), nullable=True),
        sa.Column('sin_collected_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('sin_declined', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('sin_declined_at', sa.DateTime(timezone=True), nullable=True),

        # Verification depth (denormalized for fast filtering)
        sa.Column('verification_depth', verification_depth_enum, nullable=False, server_default='none'),

        # Lead state (denormalized for marketplace queries)
        sa.Column('lead_state', lead_state_enum, nullable=False, server_default='unqualified'),
        sa.Column('lead_state_updated_at', sa.DateTime(timezone=True), nullable=True),

        # Marketing & monetization flags
        sa.Column('marketing_consent_at', sa.DateTime(timezone=True), nullable=True),  # null = no marketing
        sa.Column('cross_sell_eligible', sa.Boolean(), nullable=False, server_default='false'),
        sa.Column('marketplace_listed', sa.Boolean(), nullable=False, server_default='false'),

        # Soft-delete only (PIPEDA retention obligations)
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    )

    # ===========================================================================
    # CREATE INDEXES
    # ===========================================================================

    # Unique indexes for active (non-deleted) patients
    op.create_index(
        'idx_platform_patients_email_lower',
        'platform_patients',
        [sa.text('lower(email)')],
        unique=True,
        postgresql_where=sa.text('deleted_at IS NULL')
    )

    op.create_index(
        'idx_platform_patients_phone',
        'platform_patients',
        ['phone_e164'],
        unique=True,
        postgresql_where=sa.text('deleted_at IS NULL')
    )

    # Performance indexes for common queries
    op.create_index('idx_platform_patients_verification_depth', 'platform_patients', ['verification_depth'])
    op.create_index('idx_platform_patients_lead_state', 'platform_patients', ['lead_state'])

    # ===========================================================================
    # CREATE TRIGGER FOR UPDATED_AT
    # ===========================================================================

    op.execute("""
        CREATE OR REPLACE FUNCTION update_platform_patients_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)

    op.execute("""
        CREATE TRIGGER platform_patients_updated_at
        BEFORE UPDATE ON platform_patients
        FOR EACH ROW
        EXECUTE FUNCTION update_platform_patients_updated_at();
    """)


def downgrade() -> None:
    # Drop trigger and function
    op.execute("DROP TRIGGER IF EXISTS platform_patients_updated_at ON platform_patients")
    op.execute("DROP FUNCTION IF EXISTS update_platform_patients_updated_at()")

    # Drop indexes
    op.drop_index('idx_platform_patients_lead_state', table_name='platform_patients')
    op.drop_index('idx_platform_patients_verification_depth', table_name='platform_patients')
    op.drop_index('idx_platform_patients_phone', table_name='platform_patients')
    op.drop_index('idx_platform_patients_email_lower', table_name='platform_patients')

    # Drop table
    op.drop_table('platform_patients')

    # Drop enum types
    op.execute("DROP TYPE IF EXISTS platform_lead_state")
    op.execute("DROP TYPE IF EXISTS platform_verification_depth")
