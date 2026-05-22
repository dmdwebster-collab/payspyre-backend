"""Create platform_verifications table

Revision ID: 019_create_platform_verifications
Revises: 018_create_platform_credit_applications
Create Date: 2026-05-21

Part of PR P1: Platform Foundation Migrations

This migration creates the platform_verifications table, which stores a row
per *attempted* verification across all types. Links to type-specific tables.

Key Features:
- Unified verification tracking across all types (kyc_id, bank_link, bureau_soft, etc.)
- Links to kyc_sessions for ID verifications via vendor_session_ref
- Links to bank_links, bureau_pulls when those are added (future PRs)
- Consent ID linking for compliance (every verification ties back to a consent)
- Cost tracking for unit economics
- Status lifecycle (pending, in_progress, passed, failed, expired)

Security Notes:
- vendor_session_ref is FK-like reference into type-specific tables
- No raw vendor responses in this table (stored in type-specific tables)
- Consent required before any verification initiates

Hard Rules:
- Every verification must have a consent_id
- Consent granularity enforced (spec §8.1)
- Vendor abstraction: adapters swappable via config alone

Open Questions Referenced:
- None for MVP — mock adapters used until PR P8-P12 (real vendor integrations)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '019_create_platform_verifications'
down_revision: Union[str, None] = '018_create_platform_credit_applications'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ===========================================================================
    # CREATE NEW ENUM TYPES
    # ===========================================================================

    # platform_verifications.verification_type enum
    op.execute("""
        CREATE TYPE platform_verification_type AS ENUM (
            'kyc_id',
            'bank_link',
            'bureau_soft',
            'bureau_hard',
            'income_attestation',
            'address_proof'
        )
    """)

    # platform_verifications.status enum
    op.execute("""
        CREATE TYPE platform_verification_status AS ENUM (
            'pending',
            'in_progress',
            'passed',
            'failed',
            'expired'
        )
    """)

    # Create enum types for use in columns
    verification_type_enum = postgresql.ENUM(name='platform_verification_type', create_type=False)
    status_enum = postgresql.ENUM(name='platform_verification_status', create_type=False)

    # ===========================================================================
    # CREATE platform_verifications TABLE
    # ===========================================================================

    op.create_table(
        'platform_verifications',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('patient_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('platform_patients.id'), nullable=False),
        sa.Column('application_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('platform_credit_applications.id'), nullable=True),
        # nullable for standalone verifications (marketplace, cross-sell)
        sa.Column('verification_type', verification_type_enum, nullable=False),
        sa.Column('status', status_enum, nullable=False, server_default='pending'),
        sa.Column('vendor', sa.String(), nullable=True),
        # 'didit' | 'persona' | 'flinks' | 'inverite' | 'equifax_ca' | 'transunion_ca'
        sa.Column('vendor_session_ref', sa.String(), nullable=True),
        # FK-like reference into kyc_sessions / bank_links / bureau_pulls
        sa.Column('cost_cents', sa.Integer(), nullable=True),
        # vendor cost (for unit economics tracking)
        sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('consent_id', postgresql.UUID(as_uuid=True), nullable=True),
        # explicit consent that authorized this verification
    )

    # ===========================================================================
    # CREATE INDEXES
    # ===========================================================================

    # Patient verifications lookup
    op.create_index('idx_platform_verifications_patient', 'platform_verifications', ['patient_id'])

    # Application verifications lookup
    op.create_index('idx_platform_verifications_application', 'platform_verifications', ['application_id'])

    # Type and status filtering
    op.create_index('idx_platform_verifications_type_status', 'platform_verifications', ['verification_type', 'status'])

    # ===========================================================================
    # CREATE FOREIGN KEY TO CONSENTS (will be added in next migration)
    # ===========================================================================
    # Note: consent_id FK deferred to 021_create_platform_consents to avoid circular dependency


def downgrade() -> None:
    # Drop indexes
    op.drop_index('idx_platform_verifications_type_status', table_name='platform_verifications')
    op.drop_index('idx_platform_verifications_application', table_name='platform_verifications')
    op.drop_index('idx_platform_verifications_patient', table_name='platform_verifications')

    # Drop table
    op.drop_table('platform_verifications')

    # Drop enum types
    op.execute("DROP TYPE IF EXISTS platform_verification_status")
    op.execute("DROP TYPE IF EXISTS platform_verification_type")
