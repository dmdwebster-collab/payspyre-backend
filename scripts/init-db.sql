-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Create enum types
CREATE TYPE kyc_session_status AS ENUM ('pending', 'in_progress', 'completed', 'expired', 'failed');
CREATE TYPE kyc_overall_status AS ENUM ('pass', 'fail', 'review_required');
CREATE TYPE kyc_check_type AS ENUM ('identity', 'liveness', 'aml', 'sanctions');
CREATE TYPE kyc_check_status AS ENUM ('pass', 'fail', 'pending', 'skip');
CREATE TYPE co_borrower_role AS ENUM ('guarantor', 'joint_applicant');
CREATE TYPE business_structure AS ENUM ('sole_proprietor', 'partnership', 'corporation', 'other');
CREATE TYPE kyb_review_status AS ENUM ('pending_submission', 'under_review', 'approved', 'rejected');

-- Stub tables
CREATE TABLE IF NOT EXISTS loan_applications (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS borrowers (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS vendors (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- KYC tables
CREATE TABLE IF NOT EXISTS kyc_sessions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    loan_application_id UUID NOT NULL REFERENCES loan_applications(id),
    borrower_id UUID NOT NULL REFERENCES borrowers(id),
    vendor VARCHAR(50) NOT NULL,
    vendor_session_id VARCHAR(255),
    verification_url TEXT,
    status kyc_session_status NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ,
    metadata JSONB
);

CREATE INDEX idx_kyc_sessions_loan_app ON kyc_sessions(loan_application_id);
CREATE INDEX idx_kyc_sessions_status ON kyc_sessions(status);
CREATE INDEX idx_kyc_sessions_vendor ON kyc_sessions(vendor);

CREATE TABLE IF NOT EXISTS kyc_results (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    kyc_session_id UUID NOT NULL REFERENCES kyc_sessions(id) ON DELETE CASCADE,
    vendor VARCHAR(50) NOT NULL,
    overall_status kyc_overall_status NOT NULL,
    check_type kyc_check_type NOT NULL,
    check_status kyc_check_status NOT NULL,
    check_details JSONB,
    score NUMERIC(5,4),
    flags JSONB[],
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_kyc_results_session ON kyc_results(kyc_session_id);
CREATE INDEX idx_kcy_results_status ON kyc_results(overall_status);

CREATE TABLE IF NOT EXISTS kyc_events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    kyc_session_id UUID NOT NULL REFERENCES kyc_sessions(id) ON DELETE CASCADE,
    event_type VARCHAR(100) NOT NULL,
    vendor_event_id VARCHAR(255),
    payload JSONB NOT NULL,
    processed_at TIMESTAMPTZ DEFAULT NOW(),
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_kyc_events_session ON kyc_events(kyc_session_id);
CREATE INDEX idx_kyc_events_type ON kyc_events(event_type);

CREATE TABLE IF NOT EXISTS kyc_co_borrower_links (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    loan_application_id UUID NOT NULL REFERENCES loan_applications(id),
    primary_kyc_session_id UUID NOT NULL REFERENCES kyc_sessions(id),
    co_borrower_kyc_session_id UUID NOT NULL REFERENCES kyc_sessions(id),
    co_borrower_role co_borrower_role NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_kyc_co_borrower_loan ON kyc_co_borrower_links(loan_application_id);

CREATE TABLE IF NOT EXISTS manual_kyb_reviews (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    vendor_id UUID NOT NULL REFERENCES vendors(id),
    business_name VARCHAR(255) NOT NULL,
    business_structure business_structure NOT NULL,
    business_registration_number VARCHAR(100),
    status kyb_review_status NOT NULL DEFAULT 'pending_submission',
    submitted_by UUID REFERENCES users(id),
    reviewed_by UUID REFERENCES users(id),
    documents JSONB[],
    beneficial_owners JSONB[],
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_manual_kyb_vendor ON manual_kyb_reviews(vendor_id);
CREATE INDEX idx_manual_kyb_status ON manual_kyb_reviews(status);

-- Enable update trigger for updated_at
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER update_kyc_sessions_updated_at
    BEFORE UPDATE ON kyc_sessions
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_manual_kyb_reviews_updated_at
    BEFORE UPDATE ON manual_kyb_reviews
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();