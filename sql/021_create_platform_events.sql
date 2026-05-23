-- ============================================================
-- Migration 021_create_platform_events.sql
-- Platform-wide append-only event log with WORM protection
-- ============================================================

-- Create platform_events table
CREATE TABLE platform_events (
    id BIGSERIAL PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    patient_id UUID REFERENCES platform_patients(id),
    application_id UUID REFERENCES platform_credit_applications(id),
    event_type VARCHAR NOT NULL,
    actor VARCHAR NOT NULL,
    payload JSONB NOT NULL,
    correlation_id UUID
);

-- Create indexes
CREATE INDEX idx_platform_events_patient ON platform_events (patient_id, occurred_at DESC);
CREATE INDEX idx_platform_events_application ON platform_events (application_id, occurred_at DESC);
CREATE INDEX idx_platform_events_type ON platform_events (event_type, occurred_at DESC);
CREATE INDEX idx_platform_events_correlation ON platform_events (correlation_id);

-- Create WORM protection trigger function
CREATE OR REPLACE FUNCTION prevent_platform_events_modification()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'Cannot modify platform_events table - append-only audit trail (WORM)';
END;
$$ LANGUAGE plpgsql;

-- Create WORM trigger
CREATE TRIGGER platform_events_append_only
BEFORE UPDATE OR DELETE ON platform_events
FOR EACH ROW
EXECUTE FUNCTION prevent_platform_events_modification();

-- Add documentation comments
COMMENT ON TABLE platform_events IS 'Platform-wide append-only event log. WORM protected by trigger. CRITICAL: Do NOT drop trigger platform_events_append_only. Trigger prevents UPDATE/DELETE for FINTRAC/PIPEDA/SOX compliance.';

COMMENT ON FUNCTION prevent_platform_events_modification() IS 'Security function: prevents modification of platform_events table. Enforces WORM (Write-Once-Read-Many) for audit compliance.';

-- Mark migration as complete
INSERT INTO alembic_version (version_num) 
VALUES ('021_create_platform_events');
