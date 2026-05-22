"""Create platform_events table with append-only protection

Revision ID: 021_create_platform_events
Revises: 020_create_platform_consents
Create Date: 2026-05-21

Part of PR P1: Platform Foundation Migrations

This migration creates the platform_events table, which is the platform-wide
append-only event log. Mirrors the design of kyc_events.

CRITICAL SECURITY MIGRATION

This migration implements the platform event log with WORM (Write-Once-Read-Many)
protection. This is the authoritative audit trail for all platform operations.

Key Features:
- Platform-wide event logging for all operations
- Append-only enforcement via database trigger
- Links to patients and applications for correlation
- Event type taxonomy for all platform operations
- Actor tracking (system, patient, operator, clinic, vendor)
- Correlation ID for tracing single flows across events

Security Notes:
- platform_events is APPEND-ONLY (WORM) - UPDATE/DELETE blocked at trigger level
- All modifications to platform state must generate events
- Trigger prevents ANY updates or deletes to event rows
- Test must verify trigger is active and functional

Hard Rules:
- Append-only tables stay append-only (Hard Rule #1 from spec §12)
- Every state change, consent action, verification, decision generates event
- No PII in event payloads (use IDs, not names/DOBs/SINs)
- correlation_id links events from a single flow across time

Test Requirements:
- Test must attempt UPDATE and DELETE on platform_events and verify trigger blocks
- Test must verify event is created for each state transition
- Test must verify correlation_id correctly links related events

Compliance References:
- FINTRAC audit trail requirements
- PIPEDA record-keeping obligations
- SOX-style audit trail for financial transactions
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '021_create_platform_events'
down_revision: Union[str, None] = '020_create_platform_consents'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ===========================================================================
    # CREATE platform_events TABLE
    # ===========================================================================

    op.create_table(
        'platform_events',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.text('NOW()'), nullable=False),
        sa.Column('patient_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('platform_patients.id'), nullable=True),
        sa.Column('application_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('platform_credit_applications.id'), nullable=True),
        sa.Column('event_type', sa.String(), nullable=False),
        # 'patient.created' | 'patient.field_updated' | 'application.started'
        # | 'application.status_changed' | 'verification.requested'
        # | 'verification.completed' | 'consent.granted' | 'consent.revoked'
        # | 'decision.made' | 'lead.state_changed' | 'marketplace.listed' | etc.
        sa.Column('actor', sa.String(), nullable=False),
        # 'system' | 'patient:<id>' | 'operator:<id>' | 'vendor:<name>'
        sa.Column('payload', postgresql.JSONB(), nullable=False),
        # event-specific data
        sa.Column('correlation_id', postgresql.UUID(as_uuid=True), nullable=True),
        # for tracing a single flow across events
    )

    # ===========================================================================
    # CREATE INDEXES
    # ===========================================================================

    # Patient event timeline (most recent first)
    op.create_index(
        'idx_platform_events_patient',
        'platform_events',
        ['patient_id', sa.text('occurred_at DESC')]
    )

    # Application event timeline
    op.create_index(
        'idx_platform_events_application',
        'platform_events',
        ['application_id', sa.text('occurred_at DESC')]
    )

    # Event type queries (with time ordering)
    op.create_index(
        'idx_platform_events_type',
        'platform_events',
        ['event_type', sa.text('occurred_at DESC')]
    )

    # Correlation ID lookup for tracing flows
    op.create_index('idx_platform_events_correlation', 'platform_events', ['correlation_id'])

    # ===========================================================================
    # CRITICAL: IMPLEMENT WORM CONSTRAINT AT DATABASE LEVEL
    # ===========================================================================

    # This trigger prevents ANY modifications to platform_events after insertion
    # This enforces append-only behavior at the database level for audit compliance

    op.execute("""
        CREATE OR REPLACE FUNCTION prevent_platform_events_modification()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION 'Cannot modify platform_events table - append-only audit trail (WORM)';
        END;
        $$ LANGUAGE plpgsql;
    """)

    op.execute("""
        CREATE TRIGGER platform_events_append_only
        BEFORE UPDATE OR DELETE ON platform_events
        FOR EACH ROW
        EXECUTE FUNCTION prevent_platform_events_modification();
    """)

    # ===========================================================================
    # CREATE COMMENT TO DOCUMENT SECURITY CONSTRAINT
    # ===========================================================================

    op.execute("""
        COMMENT ON TABLE platform_events IS
        'Platform-wide append-only event log. WORM protected by trigger.
        CRITICAL: Do NOT drop trigger platform_events_append_only.
        Trigger prevents UPDATE/DELETE for FINTRAC/PIPEDA/SOX compliance.';
    """)

    op.execute("""
        COMMENT ON FUNCTION prevent_platform_events_modification() IS
        'Security function: prevents modification of platform_events table.
        Enforces WORM (Write-Once-Read-Many) for audit compliance.';
    """)


def downgrade() -> None:
    # Drop comments
    op.execute("COMMENT ON FUNCTION prevent_platform_events_modification() IS NULL")
    op.execute("COMMENT ON TABLE platform_events IS NULL")

    # CRITICAL: Remove WORM protection LAST
    op.execute("DROP TRIGGER IF EXISTS platform_events_append_only ON platform_events")
    op.execute("DROP FUNCTION IF EXISTS prevent_platform_events_modification()")

    # Drop indexes
    op.drop_index('idx_platform_events_correlation', table_name='platform_events')
    op.drop_index('idx_platform_events_type', table_name='platform_events')
    op.drop_index('idx_platform_events_application', table_name='platform_events')
    op.drop_index('idx_platform_events_patient', table_name='platform_events')

    # Drop table
    op.drop_table('platform_events')
