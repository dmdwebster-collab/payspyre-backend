"""Create platform_consents table

Revision ID: 020_create_platform_consents
Revises: 019_create_platform_verifications
Create Date: 2026-05-21

Part of PR P1: Platform Foundation Migrations

This migration creates the platform_consents table, which stores per-purpose,
granular consent. Critical for compliance — every verification ties back to a
consent row.

Key Features:
- Per-purpose consent (id_verification, bank_verification, soft_bureau_pull, etc.)
- Consent text immutability: exact text and version never updated
- Revocation tracking with timestamp
- IP address and user agent for audit trail
- Application context (nullable for marketplace/cross-sell consents)

Security Notes:
- consent_text_shown and consent_text_version are NEVER UPDATEd
- New consent language = new version, old consents preserved verbatim
- This is non-negotiable for class-action defense

Hard Rules:
- Never bundle: every distinct purpose gets its own checkbox
- Consent text shown is immutable: versioning, not editing
- All consents linked to patient and optionally application

Open Questions Referenced:
- None for MVP — consent service (PR P5) implements filesystem-loaded consent text

Compliance References:
- PIPEDA (Canadian privacy law)
- CASL (Canadian anti-spam legislation)
- Spec §8.1 (granular consent UI rule)
- Spec §8.2 (consent versioning)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '020_create_platform_consents'
down_revision: Union[str, None] = '019_create_platform_verifications'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ===========================================================================
    # CREATE NEW ENUM TYPES
    # ===========================================================================

    # platform_consents.purpose enum
    op.execute("""
        CREATE TYPE platform_consent_purpose AS ENUM (
            'id_verification',
            'bank_verification',
            'soft_bureau_pull',
            'hard_bureau_pull',
            'automated_decision_making',
            'marketing_email',
            'marketplace_listing',
            'cross_sell_eligibility',
            'aggregate_data_use'
        )
    """)

    # Create enum type for use in column
    purpose_enum = postgresql.ENUM(name='platform_consent_purpose', create_type=False)

    # ===========================================================================
    # CREATE platform_consents TABLE
    # ===========================================================================

    op.create_table(
        'platform_consents',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('patient_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('platform_patients.id'), nullable=False),
        sa.Column('purpose', purpose_enum, nullable=False),
        sa.Column('consent_granted', sa.Boolean(), nullable=False),
        sa.Column('consent_text_shown', sa.Text(), nullable=False),
        # IMMUTABLE — exact text the user saw
        sa.Column('consent_text_version', sa.String(), nullable=False),
        # e.g. 'id_verif_v2_2026-05'
        sa.Column('granted_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ip_address', postgresql.INET(), nullable=True),
        sa.Column('user_agent', sa.String(), nullable=True),
        sa.Column('application_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('platform_credit_applications.id'), nullable=True),
        # nullable
    )

    # ===========================================================================
    # CREATE INDEXES
    # ===========================================================================

    # Patient's active consents (non-revoked)
    op.create_index(
        'idx_platform_consents_patient_purpose',
        'platform_consents',
        ['patient_id', 'purpose'],
        postgresql_where=sa.text('revoked_at IS NULL')
    )

    # Application consents lookup
    op.create_index('idx_platform_consents_application', 'platform_consents', ['application_id'])

    # ===========================================================================
    # UPDATE platform_verifications TO ADD CONSENT FK
    # ===========================================================================

    # Now add the FK constraint to platform_verifications.consent_id
    op.create_foreign_key(
        'fk_platform_verifications_consent',
        'platform_verifications', 'platform_consents',
        ['consent_id'], ['id']
    )


def downgrade() -> None:
    # Drop FK from platform_verifications
    op.drop_constraint('fk_platform_verifications_consent', 'platform_verifications', type_='foreignkey')

    # Drop indexes
    op.drop_index('idx_platform_consents_application', table_name='platform_consents')
    op.drop_index('idx_platform_consents_patient_purpose', table_name='platform_consents')

    # Drop table
    op.drop_table('platform_consents')

    # Drop enum type
    op.execute("DROP TYPE IF EXISTS platform_consent_purpose")
