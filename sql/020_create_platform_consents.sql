2026-05-22T10:49:14.489480Z [info     ] db_logging_enabled             slow_threshold_ms=500
BEGIN;

-- Running upgrade 020_create_platform_consents -> 021_create_platform_events

CREATE TABLE platform_events (
    id BIGSERIAL NOT NULL, 
    occurred_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL, 
    patient_id UUID, 
    application_id UUID, 
    event_type VARCHAR NOT NULL, 
    actor VARCHAR NOT NULL, 
    payload JSONB NOT NULL, 
    correlation_id UUID, 
    PRIMARY KEY (id), 
    FOREIGN KEY(patient_id) REFERENCES platform_patients (id), 
    FOREIGN KEY(application_id) REFERENCES platform_credit_applications (id)
);

CREATE INDEX idx_platform_events_patient ON platform_events (patient_id, occurred_at DESC);

CREATE INDEX idx_platform_events_application ON platform_events (application_id, occurred_at DESC);

CREATE INDEX idx_platform_events_type ON platform_events (event_type, occurred_at DESC);

CREATE INDEX idx_platform_events_correlation ON platform_events (correlation_id);

CREATE OR REPLACE FUNCTION prevent_platform_events_modification()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION 'Cannot modify platform_events table - append-only audit trail (WORM)';
        END;
        $$ LANGUAGE plpgsql;;

CREATE TRIGGER platform_events_append_only
        BEFORE UPDATE OR DELETE ON platform_events
        FOR EACH ROW
        EXECUTE FUNCTION prevent_platform_events_modification();;

COMMENT ON TABLE platform_events IS
        'Platform-wide append-only event log. WORM protected by trigger.
        CRITICAL: Do NOT drop trigger platform_events_append_only.
        Trigger prevents UPDATE/DELETE for FINTRAC/PIPEDA/SOX compliance.';;

COMMENT ON FUNCTION prevent_platform_events_modification() IS
        'Security function: prevents modification of platform_events table.
        Enforces WORM (Write-Once-Read-Many) for audit compliance.';;

UPDATE alembic_version SET version_num='021_create_platform_events' WHERE alembic_version.version_num = '020_create_platform_consents';

COMMIT;

