"""Create platform_patient_fields table

Revision ID: 016_create_platform_patient_fields
Revises: 015_create_platform_patients
Create Date: 2026-05-21

Part of PR P1: Platform Foundation Migrations

This migration creates the platform_patient_fields table, which stores every
fielded value the patient has with source tagging. This is the source-of-truth
audit trail for all patient data.

Key Features:
- Every field value with source tagging (self_reported, practice_prefill, id_doc, etc.)
- JSONB field_value for flexible storage of strings, numbers, structured values
- Source event ID linking to kyc_events or platform_events for traceability
- Confidence score (0.00-1.00) for data quality assessment
- Current/superseded tracking for audit trail
- Supersede-by chain for field value history

Security Notes:
- Source tagging prevents silent overwrites of patient data
- is_current flag for fast queries; superseded_at for audit trail
- Never DELETE from this table — updates are inserts with supersede

Hard Rules:
- Updates are inserts: old row gets is_current=false, superseded_at=now()
- Never DELETE rows — append-only for audit compliance
- Self-reported values never silently overwritten by aggregator-derived values
- Review UI shows both sources when discrepancies exist

Open Questions Referenced:
- Q7 (practice-provided prefill vs self-report): source tagging enables discrepancy detection (spec §8.4)
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '016_create_platform_patient_fields'
down_revision: Union[str, None] = '015_create_platform_patients'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ===========================================================================
    # CREATE platform_patient_fields TABLE
    # ===========================================================================

    op.create_table(
        'platform_patient_fields',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('patient_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('platform_patients.id'), nullable=False),
        sa.Column('field_key', sa.String(), nullable=False),
        # 'legal_first_name', 'address_street', 'employer_name',
        # 'monthly_income_after_tax', 'time_at_address_months', etc.
        sa.Column('field_value', postgresql.JSONB(), nullable=False),
        # jsonb so we can store strings, numbers, structured values
        sa.Column('source', sa.String(), nullable=False),
        # 'self_reported' | 'practice_prefill' | 'id_doc' | 'bureau_soft'
        # | 'bureau_hard' | 'bank_aggregator' | 'didit_kyc' | 'manual_override'
        sa.Column('source_event_id', sa.BigInteger(), nullable=True),
        # FK to kyc_events or platform_events for traceability
        sa.Column('confidence', sa.Numeric(3, 2), nullable=True),
        # 0.00-1.00 where applicable
        sa.Column('verified_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('is_current', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('superseded_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('superseded_by_id', sa.BigInteger(), nullable=True),
    )

    # ===========================================================================
    # CREATE INDEXES
    # ===========================================================================

    # Primary query index: current values for a patient's fields
    op.create_index(
        'idx_platform_patient_fields_current',
        'platform_patient_fields',
        ['patient_id', 'field_key'],
        postgresql_where=sa.text('is_current = true')
    )

    # Source-based queries for audit trail
    op.create_index('idx_platform_patient_fields_source', 'platform_patient_fields', ['source'])

    # Self-referential FK for supersede chain
    op.create_foreign_key(
        'fk_platform_patient_fields_superseded_by',
        'platform_patient_fields', 'platform_patient_fields',
        ['superseded_by_id'], ['id']
    )


def downgrade() -> None:
    # Drop foreign key
    op.drop_constraint('fk_platform_patient_fields_superseded_by', 'platform_patient_fields', type_='foreignkey')

    # Drop indexes
    op.drop_index('idx_platform_patient_fields_source', table_name='platform_patient_fields')
    op.drop_index('idx_platform_patient_fields_current', table_name='platform_patient_fields')

    # Drop table
    op.drop_table('platform_patient_fields')
