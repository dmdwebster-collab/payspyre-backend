-- ============================================================
-- PaySpyre v2 — Platform Foundation Migrations (PR P1)
-- Auto-generated from Alembic migrations 015-021
-- Date: 2026-05-22
--
-- This file contains all 7 platform table migrations in execution order.
-- Execute the entire file in Supabase Dashboard SQL Editor.
--
-- IMPORTANT: Execute as a single script. All migrations must succeed.
-- ============================================================

-- ============================================================
-- Migration 015: 015_create_platform_patients.py
-- Platform patients table — canonical human record
-- ============================================================

CREATE TYPE platform_verification_depth AS ENUM (
    'none',
    'email_verified',
    'phone_verified',
    'id_verified',
    'id_bank_verified',
    'id_bank_cb_verified'
);

CREATE TYPE platform_lead_state AS ENUM (
    'unqualified',
    'pre_qualified',
    'pre_approved',
    'approved',
    'declined'
);

CREATE TABLE platform_patients (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    legal_first_name TEXT,
    legal_last_name TEXT,
    dob DATE,
    email TEXT,
    phone_e164 TEXT,

    sin_encrypted BYTEA,
    sin_last3 CHAR(3),
    sin_collected_at TIMESTAMPTZ,
    sin_declined BOOLEAN NOT NULL DEFAULT false,
    sin_declined_at TIMESTAMPTZ,

    verification_depth platform_verification_depth NOT NULL DEFAULT 'none',

    lead_state platform_lead_state NOT NULL DEFAULT 'unqualified',
    lead_state_updated_at TIMESTAMPTZ,

    marketing_consent_at TIMESTAMPTZ,
    cross_sell_eligible BOOLEAN NOT NULL DEFAULT false,
    marketplace_listed BOOLEAN NOT NULL DEFAULT false,

    deleted_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX idx_platform_patients_email_lower ON platform_patients (lower(email)) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX idx_platform_patients_phone ON platform_patients (phone_e164) WHERE deleted_at IS NULL;
CREATE INDEX idx_platform_patients_verification_depth ON platform_patients (verification_depth);
CREATE INDEX idx_platform_patients_lead_state ON platform_patients (lead_state);

CREATE OR REPLACE FUNCTION update_platform_patients_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER platform_patients_updated_at
BEFORE UPDATE ON platform_patients
FOR EACH ROW
EXECUTE FUNCTION update_platform_patients_updated_at();

INSERT INTO alembic_version (version_num)
VALUES ('015_create_platform_patients');

-- ============================================================
-- Migration 016: 016_create_platform_patient_fields.py
-- Source-tagged field audit trail — every field value with source
-- ============================================================

CREATE TABLE platform_patient_fields (
    id BIGSERIAL PRIMARY KEY,
    patient_id UUID NOT NULL REFERENCES platform_patients(id),
    field_key TEXT NOT NULL,
    field_value JSONB NOT NULL,
    source TEXT NOT NULL,
    source_event_id BIGINT,
    confidence NUMERIC(3,2),
    verified_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_current BOOLEAN NOT NULL DEFAULT true,
    superseded_at TIMESTAMPTZ,
    superseded_by_id BIGINT REFERENCES platform_patient_fields(id)
);

CREATE INDEX idx_platform_patient_fields_current ON platform_patient_fields (patient_id, field_key) WHERE is_current = true;
CREATE INDEX idx_platform_patient_fields_source ON platform_patient_fields (source);

ALTER TABLE platform_patient_fields ADD CONSTRAINT fk_platform_patient_fields_superseded_by
    FOREIGN KEY(superseded_by_id) REFERENCES platform_patient_fields(id);

INSERT INTO alembic_version (version_num)
VALUES ('016_create_platform_patient_fields');

-- ============================================================
-- Migration 017: 017_create_platform_credit_products.py
-- Credit product configuration — verification matrix (Dave's toggle)
-- ============================================================

CREATE TYPE platform_credit_product_status AS ENUM (
    'draft',
    'active',
    'archived'
);

CREATE TYPE platform_vertical AS ENUM (
    'dental',
    'auto',
    'veterinary'
);

CREATE TYPE platform_funding_source AS ENUM (
    'payspyre_capital',
    'partner_lender',
    'hybrid',
    'clinic_self'
);

CREATE TABLE platform_credit_products (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    vertical platform_vertical NOT NULL,
    status platform_credit_product_status NOT NULL DEFAULT 'draft',

    min_amount_cents BIGINT NOT NULL,
    max_amount_cents BIGINT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'CAD',

    verification_matrix JSONB NOT NULL,

    decision_ruleset TEXT NOT NULL,

    pricing_config JSONB NOT NULL,

    funding_source platform_funding_source NOT NULL,

    created_by UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    version INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX idx_platform_credit_products_code ON platform_credit_products (code);
CREATE INDEX idx_platform_credit_products_vertical_status ON platform_credit_products (vertical, status);

CREATE OR REPLACE FUNCTION update_platform_credit_products_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER platform_credit_products_updated_at
BEFORE UPDATE ON platform_credit_products
FOR EACH ROW
EXECUTE FUNCTION update_platform_credit_products_updated_at();

INSERT INTO alembic_version (version_num)
VALUES ('017_create_platform_credit_products');

-- ============================================================
-- Migration 018: 018_create_platform_credit_applications.py
-- Credit applications with co-applicant linkage and flow state
-- ============================================================

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
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id UUID NOT NULL REFERENCES platform_patients(id),
    credit_product_id UUID NOT NULL REFERENCES platform_credit_products(id),
    credit_product_version INTEGER NOT NULL,

    co_applicant_of_application_id UUID REFERENCES platform_credit_applications(id),
    applicant_role platform_applicant_role NOT NULL DEFAULT 'primary',

    requested_amount_cents BIGINT NOT NULL,
    requested_amount_source platform_amount_source NOT NULL,
    clinic_proposed_amount_cents BIGINT,
    patient_proposed_amount_cents BIGINT,

    vendor_id UUID REFERENCES vendors(id),
    treatment_plan_ref TEXT,

    status platform_application_status NOT NULL DEFAULT 'started',
    status_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    flow_state JSONB NOT NULL DEFAULT '{}'::jsonb,

    decision JSONB,
    decision_at TIMESTAMPTZ,
    decision_by TEXT,

    self_reported JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_platform_credit_applications_patient ON platform_credit_applications (patient_id);
CREATE INDEX idx_platform_credit_applications_status ON platform_credit_applications (status);
CREATE INDEX idx_platform_credit_applications_coapp ON platform_credit_applications (co_applicant_of_application_id) WHERE co_applicant_of_application_id IS NOT NULL;

ALTER TABLE platform_credit_applications ADD CONSTRAINT fk_platform_credit_applications_co_applicant_of
    FOREIGN KEY(co_applicant_of_application_id) REFERENCES platform_credit_applications(id);

ALTER TABLE platform_credit_applications ADD CONSTRAINT fk_platform_credit_applications_vendor
    FOREIGN KEY(vendor_id) REFERENCES vendors(id);

CREATE OR REPLACE FUNCTION update_platform_credit_applications_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER platform_credit_applications_updated_at
BEFORE UPDATE ON platform_credit_applications
FOR EACH ROW
EXECUTE FUNCTION update_platform_credit_applications_updated_at();

INSERT INTO alembic_version (version_num)
VALUES ('018_create_platform_credit_applications');

-- ============================================================
-- Migration 019: 019_create_platform_verifications.py
-- Unified verification tracking across all types
-- ============================================================

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
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id UUID NOT NULL REFERENCES platform_patients(id),
    application_id UUID REFERENCES platform_credit_applications(id),
    verification_type platform_verification_type NOT NULL,
    status platform_verification_status NOT NULL DEFAULT 'pending',
    vendor TEXT,
    vendor_session_ref TEXT,
    cost_cents INTEGER,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    consent_id UUID
);

CREATE INDEX idx_platform_verifications_patient ON platform_verifications (patient_id);
CREATE INDEX idx_platform_verifications_application ON platform_verifications (application_id);
CREATE INDEX idx_platform_verifications_type_status ON platform_verifications (verification_type, status);

INSERT INTO alembic_version (version_num)
VALUES ('019_create_platform_verifications');

-- ============================================================
-- Migration 020: 020_create_platform_consents.py
-- Granular per-purpose consent with immutable text versioning
-- ============================================================

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
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    patient_id UUID NOT NULL REFERENCES platform_patients(id),
    purpose platform_consent_purpose NOT NULL,
    consent_granted BOOLEAN NOT NULL,
    consent_text_shown TEXT NOT NULL,
    consent_text_version TEXT NOT NULL,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at TIMESTAMPTZ,
    ip_address INET,
    user_agent TEXT,
    application_id UUID REFERENCES platform_credit_applications(id)
);

CREATE INDEX idx_platform_consents_patient_purpose ON platform_consents (patient_id, purpose) WHERE revoked_at IS NULL;
CREATE INDEX idx_platform_consents_application ON platform_consents (application_id);

ALTER TABLE platform_verifications ADD CONSTRAINT fk_platform_verifications_consent
    FOREIGN KEY(consent_id) REFERENCES platform_consents(id);

INSERT INTO alembic_version (version_num)
VALUES ('020_create_platform_consents');

-- ============================================================
-- Migration 021: 021_create_platform_events.py
-- Platform-wide append-only event log with WORM protection
-- ============================================================

CREATE TABLE platform_events (
    id BIGSERIAL PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    patient_id UUID REFERENCES platform_patients(id),
    application_id UUID REFERENCES platform_credit_applications(id),
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload JSONB NOT NULL,
    correlation_id UUID
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
$$ LANGUAGE plpgsql;

CREATE TRIGGER platform_events_append_only
BEFORE UPDATE OR DELETE ON platform_events
FOR EACH ROW
EXECUTE FUNCTION prevent_platform_events_modification();

COMMENT ON TABLE platform_events IS 'Platform-wide append-only event log. WORM protected by trigger. CRITICAL: Do NOT drop trigger platform_events_append_only. Trigger prevents UPDATE/DELETE for FINTRAC/PIPEDA/SOX compliance.';

COMMENT ON FUNCTION prevent_platform_events_modification() IS 'Security function: prevents modification of platform_events table. Enforces WORM (Write-Once-Read-Many) for audit compliance.';

INSERT INTO alembic_version (version_num)
VALUES ('021_create_platform_events');

-- ============================================================
-- VERIFICATION BLOCK
-- ============================================================

DO $$
DECLARE
    table_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO table_count
    FROM information_schema.tables
    WHERE table_schema = 'public'
      AND table_name LIKE 'platform_%';

    IF table_count != 7 THEN
        RAISE EXCEPTION 'Expected 7 platform_* tables, found %', table_count;
    END IF;

    RAISE NOTICE 'SUCCESS: All 7 platform_* tables created successfully';
END $$;

INSERT INTO platform_events (event_type, actor, payload)
VALUES ('test.worm_verification', 'system', '{"test": "verification_block"}')
RETURNING id;

-- ============================================================
-- MANUAL TESTING INSTRUCTIONS
-- ============================================================
-- After executing this SQL file, manually run these tests:

-- TEST 1: Verify WORM trigger blocks UPDATE (should FAIL)
-- SET ROLE authenticated;
-- UPDATE platform_events
-- SET event_type = 'tampered'
-- WHERE event_type = 'test.worm_verification';

-- TEST 2: Verify WORM trigger blocks DELETE (should FAIL)
-- DELETE FROM platform_events
-- WHERE event_type = 'test.worm_verification';

-- TEST 3: Check 'authenticated' role grants on platform_* tables
-- SELECT grantee, privilege, table_name
-- FROM information_schema.role_table_grants
-- WHERE table_name LIKE 'platform_%'
--   AND grantee = 'authenticated'
-- ORDER BY table_name, privilege;

-- Reset role when done:
-- RESET ROLE;

-- ============================================================
-- END OF ALL_MIGRATIONS.SQL
-- Total tables: 7 platform_* tables
-- Key feature: platform_events has WORM append-only protection
-- ============================================================
