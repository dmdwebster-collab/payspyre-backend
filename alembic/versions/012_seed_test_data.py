"""Seed test data for development and testing

Revision ID: 012_seed_test_data
Revises: 006_add_row_level_security
Create Date: 2026-05-14

This migration creates comprehensive test data for PaySpyre development:
- Admin user with bcrypt-hashed password
- Two vendors (Brightside Dental Kelowna, KDC Dental)
- Borrowers with various statuses
- Loan applications in different states (draft, pending, approved, funded)
- KYC sessions and results (pass/fail/review_required)
- Documents for applications
- Sample notifications

Credentials:
- Admin: admin@payspyre.com / Admin123!
"""
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Sequence, Union
from uuid import uuid4

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '012_seed_test_data'
down_revision: Union[str, None] = '009_seed_roles_and_permissions'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Guard: Only run in development/test environments
    bind = op.get_bind()
    result = bind.execute(sa.text("SELECT current_database()")).scalar()
    if result not in ['payspyre', 'payspyre_dev', 'payspyre_test']:
        # Skip in production/staging
        return

    # Check if admin user already exists (idempotency check)
    existing_admin = bind.execute(
        sa.text("SELECT id FROM users WHERE email = 'admin@payspyre.com' LIMIT 1")
    ).scalar()
    if existing_admin:
        # Data already seeded, skip
        return

    # ========================================================================
    # GENERATE UUIDS FOR ALL ENTITIES
    # ========================================================================
    admin_user_id = uuid4()
    vendor1_id = uuid4()
    vendor2_id = uuid4()
    borrower1_id = uuid4()
    borrower2_id = uuid4()
    borrower3_id = uuid4()
    borrower4_id = uuid4()
    borrower5_id = uuid4()

    # Loan applications
    app1_id = uuid4()
    app2_id = uuid4()
    app3_id = uuid4()
    app4_id = uuid4()
    app5_id = uuid4()

    # KYC sessions
    kyc1_id = uuid4()
    kyc2_id = uuid4()
    kyc3_id = uuid4()
    kyc4_id = uuid4()
    kyc5_id = uuid4()

    # KYC results
    kyc_result_1a_id = uuid4()
    kyc_result_1b_id = uuid4()
    kyc_result_1c_id = uuid4()
    kyc_result_2a_id = uuid4()
    kyc_result_2b_id = uuid4()

    # Documents
    doc1_id = uuid4()
    doc2_id = uuid4()
    doc3_id = uuid4()
    doc4_id = uuid4()
    doc5_id = uuid4()

    # Notifications
    notif1_id = uuid4()
    notif2_id = uuid4()
    notif3_id = uuid4()

    # ========================================================================
    # INSERT ADMIN USER
    # ========================================================================
    # Pre-generated bcrypt hash for 'Admin123!' (12 rounds)
    admin_password_hash = '$2b$12$NSyGDrS3EatmPuAUEmojLuK34a8CyKbCRAVIzg/e7N6nAcfjYC7Vq'

    op.execute("""
        INSERT INTO users (id, email, password_hash, first_name, last_name, phone, is_active, is_verified, created_at, updated_at)
        VALUES (
            '{}',
            'admin@payspyre.com',
            '{}',
            'Admin',
            'User',
            '+1-250-555-0001',
            true,
            true,
            NOW(),
            NOW()
        )
    """.format(admin_user_id, admin_password_hash))

    # Assign admin role
    admin_role_id = op.get_bind().execute(
        sa.text("SELECT id FROM roles WHERE name = 'admin'")
    ).scalar()

    if admin_role_id:
        op.execute("""
            INSERT INTO user_roles (id, user_id, role_id, created_at)
            VALUES ('{}', '{}', '{}', NOW())
        """.format(uuid4(), admin_user_id, admin_role_id))

    # ========================================================================
    # INSERT VENDORS
    # ========================================================================

    # Vendor 1: Brightside Dental Kelowna - Active, KYC complete
    op.execute("""
        INSERT INTO vendors (
            id, business_name, dba_name, business_type, contact_name, email, phone,
            address_line1, address_line2, city, province, postal_code, license_number,
            license_expiry, status, compliance_score, created_at, updated_at
        ) VALUES (
            '{}',
            'Brightside Dental Kelowna',
            'Brightside Dental',
            'corporation',
            'Dr. Sarah Bright',
            'contact@brightsidedental.ca',
            '+1-250-555-0002',
            '123 Main Street',
            'Suite 100',
            'Kelowna',
            'BC',
            'V1Y 1A2',
            'BC-12345',
            '2026-12-31',
            'active',
            98.5,
            NOW(),
            NOW()
        )
    """.format(vendor1_id))

    # Vendor 2: KDC Dental - Pending, needs KYC
    op.execute("""
        INSERT INTO vendors (
            id, business_name, dba_name, business_type, contact_name, email, phone,
            address_line1, address_line2, city, province, postal_code, license_number,
            license_expiry, status, compliance_score, created_at, updated_at
        ) VALUES (
            '{}',
            'KDC Dental',
            'Kelowna Dental Centre',
            'corporation',
            'Dr. Michael Webster',
            'contact@kdcdental.ca',
            '+1-250-555-0003',
            '456 Bernard Avenue',
            NULL,
            'Kelowna',
            'BC',
            'V1Y 6H1',
            'BC-67890',
            '2026-06-30',
            'pending',
            0.0,
            NOW(),
            NOW()
        )
    """.format(vendor2_id))

    # ========================================================================
    # INSERT BORROWERS
    # ========================================================================

    # Borrower 1
    op.execute("""
        INSERT INTO borrowers (
            id, first_name, last_name, email, phone, date_of_birth,
            address_line1, address_line2, city, province, postal_code, country,
            employment_status, employer_name, employment_income,
            sin_last_3, credit_score, credit_history_months, credit_utilization,
            bank_account_verified, bank_account_last_4, created_at, updated_at
        ) VALUES (
            '{}', 'John', 'Doe', 'john.doe@example.com', '+1-250-555-0101',
            '1985-03-15',
            '123 Orchard Road', 'Apt 4B', 'Kelowna', 'BC', 'V1V 2V3', 'CA',
            'employed', 'Tech Corp', 75000.00,
            '123', 720, 96, 0.25,
            'flinks_session_001', '1234',
            NOW(), NOW()
        )
    """.format(borrower1_id))

    # Borrower 2
    op.execute("""
        INSERT INTO borrowers (
            id, first_name, last_name, email, phone, date_of_birth,
            address_line1, address_line2, city, province, postal_code, country,
            employment_status, employer_name, employment_income,
            sin_last_3, credit_score, credit_history_months, credit_utilization,
            bank_account_verified, bank_account_last_4, created_at, updated_at
        ) VALUES (
            '{}', 'Jane', 'Smith', 'jane.smith@example.com', '+1-250-555-0102',
            '1990-07-22',
            '456 Lakeshore Drive', NULL, 'Vernon', 'BC', 'V1B 3K4', 'CA',
            'self_employed', 'Smith Consulting', 95000.00,
            '456', 780, 120, 0.15,
            'flinks_session_002', '5678',
            NOW(), NOW()
        )
    """.format(borrower2_id))

    # Borrower 3
    op.execute("""
        INSERT INTO borrowers (
            id, first_name, last_name, email, phone, date_of_birth,
            address_line1, address_line2, city, province, postal_code, country,
            employment_status, employer_name, employment_income,
            sin_last_3, credit_score, credit_history_months, credit_utilization,
            bank_account_verified, bank_account_last_4, created_at, updated_at
        ) VALUES (
            '{}', 'Robert', 'Johnson', 'robert.j@example.com', '+1-250-555-0103',
            '1978-11-08',
            '789 Highland Road', 'Unit 12', 'Penticton', 'BC', 'V2A 8M9', 'CA',
            'employed', 'Regional Hospital', 65000.00,
            '789', 680, 84, 0.35,
            'flinks_session_003', '9012',
            NOW(), NOW()
        )
    """.format(borrower3_id))

    # Borrower 4
    op.execute("""
        INSERT INTO borrowers (
            id, first_name, last_name, email, phone, date_of_birth,
            address_line1, address_line2, city, province, postal_code, country,
            employment_status, employer_name, employment_income,
            sin_last_3, credit_score, credit_history_months, credit_utilization,
            bank_account_verified, bank_account_last_4, created_at, updated_at
        ) VALUES (
            '{}', 'Emily', 'Chen', 'emily.c@example.com', '+1-250-555-0104',
            '1995-02-14',
            '321 Mountain Avenue', NULL, 'Kelowna', 'BC', 'V1Y 7N8', 'CA',
            'student', NULL, 25000.00,
            '012', 710, 36, 0.40,
            'flinks_session_004', '3456',
            NOW(), NOW()
        )
    """.format(borrower4_id))

    # Borrower 5
    op.execute("""
        INSERT INTO borrowers (
            id, first_name, last_name, email, phone, date_of_birth,
            address_line1, address_line2, city, province, postal_code, country,
            employment_status, employer_name, employment_income,
            sin_last_3, credit_score, credit_history_months, credit_utilization,
            bank_account_verified, bank_account_last_4, created_at, updated_at
        ) VALUES (
            '{}', 'David', 'Thompson', 'david.t@example.com', '+1-250-555-0105',
            '1982-09-30',
            '654 Valley View', 'Suite 5', 'West Kelowna', 'BC', 'V4T 1E3', 'CA',
            'retired', NULL, 45000.00,
            '345', 755, 180, 0.10,
            'flinks_session_005', '7890',
            NOW(), NOW()
        )
    """.format(borrower5_id))

    # ========================================================================
    # INSERT LOAN APPLICATIONS
    # ========================================================================

    # Application 1: Draft - Brightside Dental
    op.execute("""
        INSERT INTO loan_applications (
            id, borrower_id, vendor_id, co_borrower_id, requested_amount, purpose,
            treatment_description, status, credit_product_code, term_months,
            interest_rate, payment_frequency, decision, decision_reason,
            submitted_at, approved_at, funded_at, created_at, updated_at
        ) VALUES (
            '{}', '{}', '{}', NULL, 5000.00, 'dental_implants',
            'Upper and lower dental implants with abutments', 'draft',
            'patient_financing_standard', 24, 0.0899, 'monthly', NULL, NULL,
            NULL, NULL, NULL, NOW(), NOW()
        )
    """.format(app1_id, borrower1_id, vendor1_id))

    # Application 2: Pending Documents - KDC Dental
    op.execute("""
        INSERT INTO loan_applications (
            id, borrower_id, vendor_id, co_borrower_id, requested_amount, purpose,
            treatment_description, status, credit_product_code, term_months,
            interest_rate, payment_frequency, decision, decision_reason,
            submitted_at, approved_at, funded_at, created_at, updated_at
        ) VALUES (
            '{}', '{}', '{}', NULL, 7500.00, 'orthodontics',
            'Full orthodontic treatment including braces', 'pending_documents',
            'patient_financing_standard', 36, 0.0999, 'monthly', NULL, NULL,
            NOW() - INTERVAL '2 days', NULL, NULL, NOW() - INTERVAL '2 days', NOW()
        )
    """.format(app2_id, borrower2_id, vendor2_id))

    # Application 3: Underwriting - Brightside Dental
    op.execute("""
        INSERT INTO loan_applications (
            id, borrower_id, vendor_id, co_borrower_id, requested_amount, purpose,
            treatment_description, status, credit_product_code, term_months,
            interest_rate, payment_frequency, decision, decision_reason,
            submitted_at, approved_at, funded_at, created_at, updated_at
        ) VALUES (
            '{}', '{}', '{}', NULL, 12000.00, 'comprehensive_restoration',
            'Full mouth restoration with crowns and bridges', 'underwriting',
            'patient_financing_premium', 48, 0.0799, 'monthly', NULL, NULL,
            NOW() - INTERVAL '5 days', NULL, NULL, NOW() - INTERVAL '5 days', NOW()
        )
    """.format(app3_id, borrower3_id, vendor1_id))

    # Application 4: Approved - Brightside Dental
    op.execute("""
        INSERT INTO loan_applications (
            id, borrower_id, vendor_id, co_borrower_id, requested_amount, purpose,
            treatment_description, status, credit_product_code, term_months,
            interest_rate, payment_frequency, decision, decision_reason,
            submitted_at, approved_at, funded_at, created_at, updated_at
        ) VALUES (
            '{}', '{}', '{}', NULL, 3000.00, 'cosmetic_dentistry',
            'Porcelain veneers for 8 front teeth', 'approved',
            'patient_financing_standard', 12, 0.0699, 'monthly', 'approved',
            'Excellent credit score, stable employment',
            NOW() - INTERVAL '1 week', NOW() - INTERVAL '2 days', NULL,
            NOW() - INTERVAL '1 week', NOW()
        )
    """.format(app4_id, borrower4_id, vendor1_id))

    # Application 5: Funded - KDC Dental
    op.execute("""
        INSERT INTO loan_applications (
            id, borrower_id, vendor_id, co_borrower_id, requested_amount, purpose,
            treatment_description, status, credit_product_code, term_months,
            interest_rate, payment_frequency, decision, decision_reason,
            submitted_at, approved_at, funded_at, created_at, updated_at
        ) VALUES (
            '{}', '{}', '{}', NULL, 8000.00, 'dental_implants',
            'All-on-4 implant supported denture', 'funded',
            'patient_financing_standard', 36, 0.0899, 'monthly', 'approved',
            'Strong credit history, adequate income',
            NOW() - INTERVAL '2 weeks', NOW() - INTERVAL '1 week', NOW() - INTERVAL '3 days',
            NOW() - INTERVAL '2 weeks', NOW()
        )
    """.format(app5_id, borrower5_id, vendor2_id))

    # ========================================================================
    # INSERT KYC SESSIONS AND RESULTS
    # ========================================================================

    # KYC Session 1: Completed - Application 1
    op.execute("""
        INSERT INTO kyc_sessions (
            id, loan_application_id, borrower_id, vendor, vendor_session_id,
            verification_url, status, expires_at, created_at, updated_at
        ) VALUES (
            '{}', '{}', '{}', 'didit', 'didit_session_001',
            'https://verification.didit.com/session/didit_session_001',
            'completed', NOW() + INTERVAL '30 days', NOW() - INTERVAL '1 day', NOW()
        )
    """.format(kyc1_id, app1_id, borrower1_id))

    # KYC Results for Session 1 (Pass)
    op.execute("""
        INSERT INTO kyc_results (
            id, kyc_session_id, vendor, overall_status, check_type, check_status,
            check_details, score, created_at
        ) VALUES (
            '{}', '{}', 'didit', 'pass', 'identity', 'pass',
            '{"name_match": true, "dob_match": true, "address_match": true}',
            0.98, NOW()
        )
    """.format(kyc_result_1a_id, kyc1_id))

    op.execute("""
        INSERT INTO kyc_results (
            id, kyc_session_id, vendor, overall_status, check_type, check_status,
            check_details, score, created_at
        ) VALUES (
            '{}', '{}', 'didit', 'pass', 'liveness', 'pass',
            '{"liveness_confidence": 0.99, "anti_spoof_score": 0.97}',
            0.99, NOW()
        )
    """.format(kyc_result_1b_id, kyc1_id))

    op.execute("""
        INSERT INTO kyc_results (
            id, kyc_session_id, vendor, overall_status, check_type, check_status,
            check_details, score, created_at
        ) VALUES (
            '{}', '{}', 'didit', 'pass', 'aml', 'pass',
            '{"pep_hit": false, "sanctions_hit": false, "adverse_media": 0}',
            0.95, NOW()
        )
    """.format(kyc_result_1c_id, kyc1_id))

    # KYC Session 2: In Progress - Application 2
    op.execute("""
        INSERT INTO kyc_sessions (
            id, loan_application_id, borrower_id, vendor, vendor_session_id,
            verification_url, status, expires_at, created_at, updated_at
        ) VALUES (
            '{}', '{}', '{}', 'persona', 'persona_session_001',
            'https://withpersona.com/verify/persona_session_001',
            'in_progress', NOW() + INTERVAL '7 days', NOW() - INTERVAL '1 hour', NOW()
        )
    """.format(kyc2_id, app2_id, borrower2_id))

    # KYC Session 3: Completed with Review Required - Application 3
    op.execute("""
        INSERT INTO kyc_sessions (
            id, loan_application_id, borrower_id, vendor, vendor_session_id,
            verification_url, status, expires_at, created_at, updated_at
        ) VALUES (
            '{}', '{}', '{}', 'didit', 'didit_session_003',
            'https://verification.didit.com/session/didit_session_003',
            'completed', NOW() + INTERVAL '30 days', NOW() - INTERVAL '2 days', NOW()
        )
    """.format(kyc3_id, app3_id, borrower3_id))

    # KYC Results for Session 3 (Review Required)
    op.execute("""
        INSERT INTO kyc_results (
            id, kyc_session_id, vendor, overall_status, check_type, check_status,
            check_details, score, created_at
        ) VALUES (
            '{}', '{}', 'didit', 'review_required', 'identity', 'pass',
            '{"name_match": true, "dob_match": true, "address_match": true}',
            0.97, NOW()
        )
    """.format(kyc_result_2a_id, kyc3_id))

    op.execute("""
        INSERT INTO kyc_results (
            id, kyc_session_id, vendor, overall_status, check_type, check_status,
            check_details, score, created_at
        ) VALUES (
            '{}', '{}', 'didit', 'review_required', 'aml', 'review_required',
            '{"pep_hit": false, "sanctions_hit": false, "adverse_media": 2, "reason": "Minor adverse media mentions"}',
            0.85, NOW()
        )
    """.format(kyc_result_2b_id, kyc3_id))

    # KYC Session 4: Completed - Application 4
    op.execute("""
        INSERT INTO kyc_sessions (
            id, loan_application_id, borrower_id, vendor, vendor_session_id,
            verification_url, status, expires_at, created_at, updated_at
        ) VALUES (
            '{}', '{}', '{}', 'persona', 'persona_session_004',
            'https://withpersona.com/verify/persona_session_004',
            'completed', NOW() + INTERVAL '30 days', NOW() - INTERVAL '3 days', NOW()
        )
    """.format(kyc4_id, app4_id, borrower4_id))

    # KYC Session 5: Completed - Application 5
    op.execute("""
        INSERT INTO kyc_sessions (
            id, loan_application_id, borrower_id, vendor, vendor_session_id,
            verification_url, status, expires_at, created_at, updated_at
        ) VALUES (
            '{}', '{}', '{}', 'didit', 'didit_session_005',
            'https://verification.didit.com/session/didit_session_005',
            'completed', NOW() + INTERVAL '30 days', NOW() - INTERVAL '1 week', NOW()
        )
    """.format(kyc5_id, app5_id, borrower5_id))

    # ========================================================================
    # INSERT DOCUMENTS
    # ========================================================================

    # Document 1: ID Front - Application 1
    op.execute("""
        INSERT INTO documents (
            id, loan_application_id, borrower_id, document_type, document_subtype,
            title, description, status, s3_object_key, s3_bucket,
            file_name, file_size_bytes, file_content_type, file_hash,
            storage_class, retention_period_days, uploaded_by, uploaded_at,
            created_at, updated_at
        ) VALUES (
            '{}', '{}', '{}', 'id_front', 'drivers_license',
            'BC Driver License Front', 'British Columbia driver license - front side',
            'verified', 'documents/{}/bc_dl_front.jpg', 'payspyre-documents-dev',
            'bc_dl_front.jpg', 245678, 'image/jpeg', 'sha256:abc123def456...',
            'STANDARD', 2555, '{}', NOW() - INTERVAL '1 day',
            NOW() - INTERVAL '1 day', NOW()
        )
    """.format(doc1_id, app1_id, borrower1_id, app1_id, admin_user_id))

    # Document 2: ID Back - Application 1
    op.execute("""
        INSERT INTO documents (
            id, loan_application_id, borrower_id, document_type, document_subtype,
            title, description, status, s3_object_key, s3_bucket,
            file_name, file_size_bytes, file_content_type, file_hash,
            storage_class, retention_period_days, uploaded_by, uploaded_at,
            created_at, updated_at
        ) VALUES (
            '{}', '{}', '{}', 'id_back', 'drivers_license',
            'BC Driver License Back', 'British Columbia driver license - back side',
            'verified', 'documents/{}/bc_dl_back.jpg', 'payspyre-documents-dev',
            'bc_dl_back.jpg', 234567, 'image/jpeg', 'sha256:def456ghi789...',
            'STANDARD', 2555, '{}', NOW() - INTERVAL '1 day',
            NOW() - INTERVAL '1 day', NOW()
        )
    """.format(doc2_id, app1_id, borrower1_id, app1_id, admin_user_id))

    # Document 3: Proof of Income - Application 3
    op.execute("""
        INSERT INTO documents (
            id, loan_application_id, borrower_id, document_type, document_subtype,
            title, description, status, s3_object_key, s3_bucket,
            file_name, file_size_bytes, file_content_type, file_hash,
            storage_class, retention_period_days, uploaded_by, uploaded_at,
            created_at, updated_at
        ) VALUES (
            '{}', '{}', '{}', 'proof_of_income', 'pay_stub',
            'Pay Stub - March 2026', 'Most recent pay stub from employer',
            'uploaded', 'documents/{}/pay_stub_march_2026.pdf', 'payspyre-documents-dev',
            'pay_stub_march_2026.pdf', 156789, 'application/pdf', 'sha256:ghi789jkl012...',
            'STANDARD', 2555, '{}', NOW() - INTERVAL '2 days',
            NOW() - INTERVAL '2 days', NOW()
        )
    """.format(doc3_id, app3_id, borrower3_id, app3_id, admin_user_id))

    # Document 4: Selfie - Application 4
    op.execute("""
        INSERT INTO documents (
            id, loan_application_id, borrower_id, document_type, document_subtype,
            title, description, status, s3_object_key, s3_bucket,
            file_name, file_size_bytes, file_content_type, file_hash,
            storage_class, retention_period_days, uploaded_by, uploaded_at,
            created_at, updated_at
        ) VALUES (
            '{}', '{}', '{}', 'selfie', NULL,
            'Selfie with ID', 'Selfie photo holding government ID',
            'verified', 'documents/{}/selfie_with_id.jpg', 'payspyre-documents-dev',
            'selfie_with_id.jpg', 345678, 'image/jpeg', 'sha256:jkl012mno345...',
            'STANDARD', 2555, '{}', NOW() - INTERVAL '3 days',
            NOW() - INTERVAL '3 days', NOW()
        )
    """.format(doc4_id, app4_id, borrower4_id, app4_id, admin_user_id))

    # Document 5: Bank Statement - Application 5
    op.execute("""
        INSERT INTO documents (
            id, loan_application_id, borrower_id, document_type, document_subtype,
            title, description, status, s3_object_key, s3_bucket,
            file_name, file_size_bytes, file_content_type, file_hash,
            storage_class, retention_period_days, uploaded_by, uploaded_at,
            created_at, updated_at
        ) VALUES (
            '{}', '{}', '{}', 'bank_statement', NULL,
            'Bank Statement - February 2026', 'Most recent bank statement',
            'verified', 'documents/{}/bank_statement_feb_2026.pdf', 'payspyre-documents-dev',
            'bank_statement_feb_2026.pdf', 234567, 'application/pdf', 'sha256:mno345pqr678...',
            'STANDARD', 2555, '{}', NOW() - INTERVAL '1 week',
            NOW() - INTERVAL '1 week', NOW()
        )
    """.format(doc5_id, app5_id, borrower5_id, app5_id, admin_user_id))

    # ========================================================================
    # INSERT NOTIFICATIONS
    # ========================================================================

    # Notification 1: Application approved
    op.execute("""
        INSERT INTO notifications (
            id, user_id, loan_application_id, type, priority, status,
            recipient, subject, body, variables, sent_at, created_at, updated_at
        ) VALUES (
            '{}', NULL, '{}', 'email', 'high', 'delivered',
            'emily.c@example.com',
            'Your loan application has been approved!',
            'Congratulations! Your loan application for $3,000.00 has been approved.',
            '{"application_id": "{}", "amount": 3000.00, "name": "Emily Chen"}',
            NOW() - INTERVAL '2 days', NOW() - INTERVAL '2 days', NOW()
        )
    """.format(notif1_id, app4_id, app4_id))

    # Notification 2: Funding complete
    op.execute("""
        INSERT INTO notifications (
            id, user_id, loan_application_id, type, priority, status,
            recipient, subject, body, variables, sent_at, created_at, updated_at
        ) VALUES (
            '{}', NULL, '{}', 'email', 'high', 'delivered',
            'david.t@example.com',
            'Your loan has been funded!',
            'Great news! Your loan of $8,000.00 has been funded and the funds are on their way to Brightside Dental.',
            '{"application_id": "{}", "amount": 8000.00, "vendor": "Brightside Dental Kelowna", "name": "David Thompson"}',
            NOW() - INTERVAL '3 days', NOW() - INTERVAL '3 days', NOW()
        )
    """.format(notif2_id, app5_id, app5_id))

    # Notification 3: Document required
    op.execute("""
        INSERT INTO notifications (
            id, user_id, loan_application_id, type, priority, status,
            recipient, subject, body, variables, sent_at, created_at, updated_at
        ) VALUES (
            '{}', NULL, '{}', 'email', 'normal', 'delivered',
            'jane.smith@example.com',
            'Additional documents required for your application',
            'Please upload your proof of income to continue with your loan application.',
            '{"application_id": "{}", "required_docs": ["proof_of_income"], "name": "Jane Smith"}',
            NOW() - INTERVAL '1 day', NOW() - INTERVAL '1 day', NOW()
        )
    """.format(notif3_id, app2_id, app2_id))

    # ========================================================================
    # INSERT NOTIFICATION PREFERENCES FOR BORROWERS
    # ========================================================================
    for borrower_id in [borrower1_id, borrower2_id, borrower3_id, borrower4_id, borrower5_id]:
        op.execute("""
            INSERT INTO notification_preferences (
                id, user_id, email_enabled, sms_enabled,
                application_status_enabled, payment_reminders_enabled,
                statements_enabled, urgent_notifications_enabled,
                marketing_enabled, daily_digest_enabled, created_at, updated_at
            ) VALUES (
                '{}', '{}', true, false, true, true, true, true, false, false,
                NOW(), NOW()
            )
        """.format(uuid4(), borrower_id))

    # ========================================================================
    # INSERT VENDOR ONBOARDING DOCUMENTS FOR VENDOR 1
    # ========================================================================
    op.execute("""
        INSERT INTO vendor_onboarding_documents (
            id, vendor_id, document_type, document_url, description,
            status, reviewed_at, review_notes, created_at, updated_at
        ) VALUES (
            '{}', '{}', 'business_license',
            'https://s3.amazonaws.com/payspyre-documents-dev/vendor/{}/business_license.pdf',
            'British Columbia business license',
            'approved', NOW() - INTERVAL '1 week', 'Valid until 2026-12-31',
            NOW() - INTERVAL '2 weeks', NOW()
        )
    """.format(uuid4(), vendor1_id, vendor1_id))

    op.execute("""
        INSERT INTO vendor_onboarding_documents (
            id, vendor_id, document_type, document_url, description,
            status, created_at, updated_at
        ) VALUES (
            '{}', '{}', 'incorporation_document',
            'https://s3.amazonaws.com/payspyre-documents-dev/vendor/{}/incorporation.pdf',
            'Certificate of incorporation',
            'approved', NOW() - INTERVAL '2 weeks', NOW()
        )
    """.format(uuid4(), vendor1_id, vendor1_id))


def downgrade() -> None:
    # Delete in reverse order of dependencies

    # Vendor onboarding documents
    op.execute("DELETE FROM vendor_onboarding_documents WHERE vendor_id IN (SELECT id FROM vendors WHERE business_name IN ('Brightside Dental Kelowna', 'KDC Dental'))")

    # Notifications
    op.execute("DELETE FROM notification_preferences WHERE user_id IN (SELECT id FROM borrowers WHERE email LIKE '%@example.com')")
    op.execute("DELETE FROM notifications WHERE loan_application_id IN (SELECT id FROM loan_applications WHERE borrower_id IN (SELECT id FROM borrowers WHERE email LIKE '%@example.com'))")

    # Documents
    op.execute("DELETE FROM documents WHERE borrower_id IN (SELECT id FROM borrowers WHERE email LIKE '%@example.com')")

    # KYC results
    op.execute("DELETE FROM kyc_results WHERE kyc_session_id IN (SELECT id FROM kyc_sessions WHERE borrower_id IN (SELECT id FROM borrowers WHERE email LIKE '%@example.com'))")

    # KYC sessions
    op.execute("DELETE FROM kyc_sessions WHERE borrower_id IN (SELECT id FROM borrowers WHERE email LIKE '%@example.com')")

    # Loan applications
    op.execute("DELETE FROM loan_applications WHERE borrower_id IN (SELECT id FROM borrowers WHERE email LIKE '%@example.com')")

    # Borrowers
    op.execute("DELETE FROM borrowers WHERE email LIKE '%@example.com'")

    # Vendors
    op.execute("DELETE FROM vendors WHERE business_name IN ('Brightside Dental Kelowna', 'KDC Dental')")

    # Admin user
    op.execute("DELETE FROM user_roles WHERE user_id IN (SELECT id FROM users WHERE email = 'admin@payspyre.com')")
    op.execute("DELETE FROM users WHERE email = 'admin@payspyre.com'")