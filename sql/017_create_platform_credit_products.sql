2026-05-22T10:49:02.759668Z [info     ] db_logging_enabled             slow_threshold_ms=500
BEGIN;

-- Running upgrade 017_create_platform_credit_products -> 018_create_platform_credit_applications

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
        );

CREATE TYPE platform_amount_source AS ENUM (
            'clinic',
            'patient',
            'clinic_then_patient_adjusted'
        );

CREATE TYPE platform_applicant_role AS ENUM (
            'primary',
            'co_applicant'
        );

CREATE TABLE platform_credit_applications (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    patient_id UUID NOT NULL, 
    credit_product_id UUID NOT NULL, 
    credit_product_version INTEGER NOT NULL, 
    co_applicant_of_application_id UUID, 
    applicant_role platform_applicant_role DEFAULT 'primary' NOT NULL, 
    requested_amount_cents BIGINT NOT NULL, 
    requested_amount_source platform_amount_source NOT NULL, 
    clinic_proposed_amount_cents BIGINT, 
    patient_proposed_amount_cents BIGINT, 
    vendor_id UUID, 
    treatment_plan_ref VARCHAR, 
    status platform_application_status DEFAULT 'started' NOT NULL, 
    status_updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL, 
    flow_state JSONB DEFAULT '{}'::jsonb NOT NULL, 
    decision JSONB, 
    decision_at TIMESTAMP WITH TIME ZONE, 
    decision_by VARCHAR, 
    self_reported JSONB DEFAULT '{}'::jsonb NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL, 
    PRIMARY KEY (id), 
    FOREIGN KEY(patient_id) REFERENCES platform_patients (id), 
    FOREIGN KEY(credit_product_id) REFERENCES platform_credit_products (id)
);

CREATE INDEX idx_platform_credit_applications_patient ON platform_credit_applications (patient_id);

CREATE INDEX idx_platform_credit_applications_status ON platform_credit_applications (status);

CREATE INDEX idx_platform_credit_applications_coapp ON platform_credit_applications (co_applicant_of_application_id) WHERE co_applicant_of_application_id IS NOT NULL;

ALTER TABLE platform_credit_applications ADD CONSTRAINT fk_platform_credit_applications_co_applicant_of FOREIGN KEY(co_applicant_of_application_id) REFERENCES platform_credit_applications (id);

ALTER TABLE platform_credit_applications ADD CONSTRAINT fk_platform_credit_applications_vendor FOREIGN KEY(vendor_id) REFERENCES vendors (id);

CREATE OR REPLACE FUNCTION update_platform_credit_applications_updated_at()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;;

CREATE TRIGGER platform_credit_applications_updated_at
        BEFORE UPDATE ON platform_credit_applications
        FOR EACH ROW
        EXECUTE FUNCTION update_platform_credit_applications_updated_at();;

UPDATE alembic_version SET version_num='018_create_platform_credit_applications' WHERE alembic_version.version_num = '017_create_platform_credit_products';

-- Running upgrade 018_create_platform_credit_applications -> 019_create_platform_verifications

CREATE TYPE platform_verification_type AS ENUM (
            'kyc_id',
            'bank_link',
            'bureau_soft',
            'bureau_hard',
            'income_attestation',
            'address_proof'
        );

CREATE TYPE platform_verification_status AS ENUM (
            'pending',
            'in_progress',
            'passed',
            'failed',
            'expired'
        );

CREATE TABLE platform_verifications (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    patient_id UUID NOT NULL, 
    application_id UUID, 
    verification_type platform_verification_type NOT NULL, 
    status platform_verification_status DEFAULT 'pending' NOT NULL, 
    vendor VARCHAR, 
    vendor_session_ref VARCHAR, 
    cost_cents INTEGER, 
    started_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL, 
    completed_at TIMESTAMP WITH TIME ZONE, 
    consent_id UUID, 
    PRIMARY KEY (id), 
    FOREIGN KEY(patient_id) REFERENCES platform_patients (id), 
    FOREIGN KEY(application_id) REFERENCES platform_credit_applications (id)
);

CREATE INDEX idx_platform_verifications_patient ON platform_verifications (patient_id);

CREATE INDEX idx_platform_verifications_application ON platform_verifications (application_id);

CREATE INDEX idx_platform_verifications_type_status ON platform_verifications (verification_type, status);

UPDATE alembic_version SET version_num='019_create_platform_verifications' WHERE alembic_version.version_num = '018_create_platform_credit_applications';

-- Running upgrade 019_create_platform_verifications -> 020_create_platform_consents

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
        );

CREATE TABLE platform_consents (
    id UUID DEFAULT gen_random_uuid() NOT NULL, 
    patient_id UUID NOT NULL, 
    purpose platform_consent_purpose NOT NULL, 
    consent_granted BOOLEAN NOT NULL, 
    consent_text_shown TEXT NOT NULL, 
    consent_text_version VARCHAR NOT NULL, 
    granted_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL, 
    revoked_at TIMESTAMP WITH TIME ZONE, 
    ip_address INET, 
    user_agent VARCHAR, 
    application_id UUID, 
    PRIMARY KEY (id), 
    FOREIGN KEY(patient_id) REFERENCES platform_patients (id), 
    FOREIGN KEY(application_id) REFERENCES platform_credit_applications (id)
);

CREATE INDEX idx_platform_consents_patient_purpose ON platform_consents (patient_id, purpose) WHERE revoked_at IS NULL;

CREATE INDEX idx_platform_consents_application ON platform_consents (application_id);

ALTER TABLE platform_verifications ADD CONSTRAINT fk_platform_verifications_consent FOREIGN KEY(consent_id) REFERENCES platform_consents (id);

UPDATE alembic_version SET version_num='020_create_platform_consents' WHERE alembic_version.version_num = '019_create_platform_verifications';

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

