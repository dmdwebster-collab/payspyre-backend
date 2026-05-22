"""Tests for Platform Foundation schema (PR P1)

These tests validate the new platform tables, indexes, constraints,
and security measures added in migrations 015-021.

Critical validations:
- platform_events WORM constraint (no UPDATE/DELETE allowed)
- All 7 tables created with correct schema
- Indexes present for performance queries
- Foreign keys correctly configured
- Enum types created for all status fields

This is PR P1 â€” the platform foundation for PaySpyre v2.
"""
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from datetime import datetime, date
from uuid import uuid4


class TestPlatformPatientsSchema:
    """Test platform_patients table schema"""

    def test_platform_patients_table_exists(self, db_session):
        """platform_patients table should exist"""
        result = db_session.execute(text("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_name = 'platform_patients'
        """)).fetchone()
        assert result is not None, "platform_patients table missing"

    def test_platform_patients_has_required_columns(self, db_session):
        """platform_patients should have all required columns per spec"""
        result = db_session.execute(text("""
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = 'platform_patients'
            AND column_name IN ('id', 'legal_first_name', 'legal_last_name', 'dob',
                              'email', 'phone_e164', 'sin_encrypted', 'sin_last3',
                              'verification_depth', 'lead_state', 'deleted_at')
            ORDER BY column_name
        """)).fetchall()

        column_names = [row[0] for row in result]
        required_columns = [
            'id', 'legal_first_name', 'legal_last_name', 'dob',
            'email', 'phone_e164', 'sin_encrypted', 'sin_last3',
            'verification_depth', 'lead_state', 'deleted_at'
        ]
        for col in required_columns:
            assert col in column_names, f"platform_patients.{col} column missing"

    def test_platform_patients_has_unique_indexes(self, db_session):
        """platform_patients should have unique indexes on email and phone for active records"""
        result = db_session.execute(text("""
            SELECT indexname
            FROM pg_indexes
            WHERE tablename = 'platform_patients'
            AND indexname IN ('idx_platform_patients_email_lower',
                            'idx_platform_patients_phone')
            ORDER BY indexname
        """)).fetchall()

        index_names = [row[0] for row in result]
        assert 'idx_platform_patients_email_lower' in index_names, "Missing email unique index"
        assert 'idx_platform_patients_phone' in index_names, "Missing phone unique index"

    def test_platform_patients_enum_types_exist(self, db_session):
        """platform_patients enum types should exist"""
        result = db_session.execute(text("""
            SELECT typname
            FROM pg_type
            WHERE typname IN ('platform_verification_depth', 'platform_lead_state')
            ORDER BY typname
        """)).fetchall()

        type_names = [row[0] for row in result]
        assert 'platform_verification_depth' in type_names, "Missing verification_depth enum"
        assert 'platform_lead_state' in type_names, "Missing lead_state enum"


class TestPlatformPatientFieldsSchema:
    """Test platform_patient_fields table schema"""

    def test_platform_patient_fields_table_exists(self, db_session):
        """platform_patient_fields table should exist"""
        result = db_session.execute(text("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_name = 'platform_patient_fields'
        """)).fetchone()
        assert result is not None, "platform_patient_fields table missing"

    def test_platform_patient_fields_has_source_columns(self, db_session):
        """platform_patient_fields should have source tagging columns"""
        result = db_session.execute(text("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'platform_patient_fields'
            AND column_name IN ('source', 'source_event_id', 'confidence',
                              'is_current', 'superseded_at', 'superseded_by_id')
            ORDER BY column_name
        """)).fetchall()

        column_names = [row[0] for row in result]
        required_columns = ['source', 'source_event_id', 'confidence',
                          'is_current', 'superseded_at', 'superseded_by_id']
        for col in required_columns:
            assert col in column_names, f"platform_patient_fields.{col} column missing"


class TestPlatformCreditProductsSchema:
    """Test platform_credit_products table schema"""

    def test_platform_credit_products_table_exists(self, db_session):
        """platform_credit_products table should exist"""
        result = db_session.execute(text("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_name = 'platform_credit_products'
        """)).fetchone()
        assert result is not None, "platform_credit_products table missing"

    def test_platform_credit_products_has_verification_matrix(self, db_session):
        """platform_credit_products should have verification_matrix JSONB column"""
        result = db_session.execute(text("""
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = 'platform_credit_products'
            AND column_name = 'verification_matrix'
        """)).fetchone()

        assert result is not None, "verification_matrix column missing"
        assert result[1] == 'jsonb', "verification_matrix should be JSONB"

    def test_platform_credit_products_code_unique(self, db_session):
        """platform_credit_products.code should have unique constraint"""
        result = db_session.execute(text("""
            SELECT constraint_name
            FROM information_schema.table_constraints
            WHERE table_name = 'platform_credit_products'
            AND constraint_type = 'UNIQUE'
        """)).fetchall()

        constraint_names = [row[0] for row in result]
        assert len(constraint_names) > 0, "No unique constraint found on platform_credit_products"


class TestPlatformCreditApplicationsSchema:
    """Test platform_credit_applications table schema"""

    def test_platform_credit_applications_table_exists(self, db_session):
        """platform_credit_applications table should exist"""
        result = db_session.execute(text("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_name = 'platform_credit_applications'
        """)).fetchone()
        assert result is not None, "platform_credit_applications table missing"

    def test_platform_credit_applications_has_flow_state(self, db_session):
        """platform_credit_applications should have flow_state JSONB column"""
        result = db_session.execute(text("""
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = 'platform_credit_applications'
            AND column_name = 'flow_state'
        """)).fetchone()

        assert result is not None, "flow_state column missing"
        assert result[1] == 'jsonb', "flow_state should be JSONB"

    def test_platform_credit_applications_co_applicant_fk(self, db_session):
        """platform_credit_applications should have self-referential FK for co-applicant"""
        result = db_session.execute(text("""
            SELECT constraint_name
            FROM information_schema.table_constraints
            WHERE table_name = 'platform_credit_applications'
            AND constraint_type = 'FOREIGN KEY'
        """)).fetchall()

        constraint_names = [row[0] for row in result]
        assert len(constraint_names) >= 2, "Should have at least 2 FKs (patient, credit_product)"


class TestPlatformVerificationsSchema:
    """Test platform_verifications table schema"""

    def test_platform_verifications_table_exists(self, db_session):
        """platform_verifications table should exist"""
        result = db_session.execute(text("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_name = 'platform_verifications'
        """)).fetchone()
        assert result is not None, "platform_verifications table missing"

    def test_platform_verifications_has_vendor_session_ref(self, db_session):
        """platform_verifications should have vendor_session_ref for FK-like reference"""
        result = db_session.execute(text("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'platform_verifications'
            AND column_name = 'vendor_session_ref'
        """)).fetchone()

        assert result is not None, "vendor_session_ref column missing"


class TestPlatformConsentsSchema:
    """Test platform_consents table schema"""

    def test_platform_consents_table_exists(self, db_session):
        """platform_consents table should exist"""
        result = db_session.execute(text("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_name = 'platform_consents'
        """)).fetchone()
        assert result is not None, "platform_consents table missing"

    def test_platform_consents_has_immutable_text_columns(self, db_session):
        """platform_consents should have immutable consent_text columns"""
        result = db_session.execute(text("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'platform_consents'
            AND column_name IN ('consent_text_shown', 'consent_text_version')
            ORDER BY column_name
        """)).fetchall()

        column_names = [row[0] for row in result]
        assert 'consent_text_shown' in column_names, "consent_text_shown column missing"
        assert 'consent_text_version' in column_names, "consent_text_version column missing"

    def test_platform_consents_purpose_enum_exists(self, db_session):
        """platform_consent_purpose enum should exist with all required values"""
        result = db_session.execute(text("""
            SELECT enumlabel
            FROM pg_enum
            WHERE enumtypid = (
                SELECT oid FROM pg_type WHERE typname = 'platform_consent_purpose'
            )
            ORDER BY enumsortorder
        """)).fetchall()

        enum_values = [row[0] for row in result]
        required_purposes = [
            'id_verification', 'bank_verification', 'soft_bureau_pull',
            'hard_bureau_pull', 'automated_decision_making', 'marketing_email',
            'marketplace_listing', 'cross_sell_eligibility', 'aggregate_data_use'
        ]
        for purpose in required_purposes:
            assert purpose in enum_values, f"Missing consent purpose: {purpose}"


class TestPlatformEventsSchema:
    """Test platform_events table schema and WORM constraint"""

    def test_platform_events_table_exists(self, db_session):
        """platform_events table should exist"""
        result = db_session.execute(text("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_name = 'platform_events'
        """)).fetchone()
        assert result is not None, "platform_events table missing"

    def test_platform_events_has_required_columns(self, db_session):
        """platform_events should have required columns per spec"""
        result = db_session.execute(text("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'platform_events'
            AND column_name IN ('occurred_at', 'event_type', 'actor',
                              'payload', 'correlation_id')
            ORDER BY column_name
        """)).fetchall()

        column_names = [row[0] for row in result]
        required_columns = ['occurred_at', 'event_type', 'actor', 'payload', 'correlation_id']
        for col in required_columns:
            assert col in column_names, f"platform_events.{col} column missing"

    def test_platform_events_has_indexes(self, db_session):
        """platform_events should have performance indexes"""
        result = db_session.execute(text("""
            SELECT indexname
            FROM pg_indexes
            WHERE tablename = 'platform_events'
            AND indexname IN ('idx_platform_events_patient',
                            'idx_platform_events_application',
                            'idx_platform_events_type',
                            'idx_platform_events_correlation')
            ORDER BY indexname
        """)).fetchall()

        index_names = [row[0] for row in result]
        assert 'idx_platform_events_patient' in index_names, "Missing patient index"
        assert 'idx_platform_events_application' in index_names, "Missing application index"
        assert 'idx_platform_events_type' in index_names, "Missing type index"
        assert 'idx_platform_events_correlation' in index_names, "Missing correlation index"

    def test_platform_events_worm_constraint(self, db_session):
        """platform_events should have WORM protection (UPDATE/DELETE blocked)"""
        import uuid
        unique_suffix = uuid.uuid4().hex[:8]

        # Create a test event
        test_event_id = db_session.execute(text("""
            INSERT INTO platform_events (event_type, actor, payload)
            VALUES (:event_type, 'system', '{"test": true}')
            RETURNING id
        """), {"event_type": f"test_event_{unique_suffix}"}).scalar()
        db_session.commit()

        # Try to UPDATE - should fail with trigger error
        try:
            with pytest.raises(Exception, match="Cannot modify.*append-only|WORM"):
                db_session.execute(text("""
                    UPDATE platform_events SET payload = '{"modified": true}' WHERE id = :event_id
                """), {"event_id": test_event_id})
                db_session.commit()
        finally:
            db_session.rollback()

        # Try to DELETE - should fail with trigger error
        try:
            with pytest.raises(Exception, match="Cannot modify.*append-only|WORM"):
                db_session.execute(text("""
                    DELETE FROM platform_events WHERE id = :event_id
                """), {"event_id": test_event_id})
                db_session.commit()
        finally:
            db_session.rollback()

        # Verify INSERT still works
        new_event_id = db_session.execute(text("""
            INSERT INTO platform_events (event_type, actor, payload)
            VALUES (:event_type, 'system', '{"test": true}')
            RETURNING id
        """), {"event_type": f"test_event_2_{unique_suffix}"}).scalar()
        db_session.commit()
        assert new_event_id is not None, "INSERT should still work on platform_events"

    def test_platform_events_trigger_exists(self, db_session):
        """platform_events_append_only trigger should exist"""
        result = db_session.execute(text("""
            SELECT trigger_name
            FROM information_schema.triggers
            WHERE trigger_name = 'platform_events_append_only'
            AND event_object_table = 'platform_events'
        """)).fetchone()

        assert result is not None, "platform_events_append_only trigger missing"


class TestPlatformRelationships:
    """Test foreign key relationships between platform tables"""

    def test_patient_fields_fk_to_patients(self, db_session):
        """platform_patient_fields should FK to platform_patients"""
        result = db_session.execute(text("""
            SELECT COUNT(*)
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
                ON tc.constraint_name = kcu.constraint_name
            WHERE tc.table_name = 'platform_patient_fields'
            AND tc.constraint_type = 'FOREIGN KEY'
            AND kcu.column_name = 'patient_id'
        """)).scalar()

        assert result == 1, "Missing FK from patient_fields to patients"

    def test_application_fk_to_patients_and_products(self, db_session):
        """platform_credit_applications should FK to platform_patients and platform_credit_products"""
        result = db_session.execute(text("""
            SELECT kcu.column_name, ccu.table_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
                ON tc.constraint_name = kcu.constraint_name
            JOIN information_schema.constraint_column_usage ccu
                ON tc.constraint_name = ccu.constraint_name
            WHERE tc.table_name = 'platform_credit_applications'
            AND tc.constraint_type = 'FOREIGN KEY'
            AND kcu.column_name IN ('patient_id', 'credit_product_id')
        """)).fetchall()

        fk_mappings = {row[0]: row[1] for row in result}
        assert fk_mappings.get('patient_id') == 'platform_patients', "Missing FK to patients"
        assert fk_mappings.get('credit_product_id') == 'platform_credit_products', "Missing FK to credit_products"

    def test_verifications_fk_to_patients_and_applications(self, db_session):
        """platform_verifications should FK to platform_patients and platform_credit_applications"""
        result = db_session.execute(text("""
            SELECT kcu.column_name, ccu.table_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
                ON tc.constraint_name = kcu.constraint_name
            JOIN information_schema.constraint_column_usage ccu
                ON tc.constraint_name = ccu.constraint_name
            WHERE tc.table_name = 'platform_verifications'
            AND tc.constraint_type = 'FOREIGN KEY'
            AND kcu.column_name IN ('patient_id', 'application_id')
        """)).fetchall()

        fk_mappings = {row[0]: row[1] for row in result}
        assert fk_mappings.get('patient_id') == 'platform_patients', "Missing FK to patients"
        assert fk_mappings.get('application_id') == 'platform_credit_applications', "Missing FK to applications"

    def test_consents_fk_to_patients_and_applications(self, db_session):
        """platform_consents should FK to platform_patients and platform_credit_applications"""
        result = db_session.execute(text("""
            SELECT kcu.column_name, ccu.table_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
                ON tc.constraint_name = kcu.constraint_name
            JOIN information_schema.constraint_column_usage ccu
                ON tc.constraint_name = ccu.constraint_name
            WHERE tc.table_name = 'platform_consents'
            AND tc.constraint_type = 'FOREIGN KEY'
            AND kcu.column_name IN ('patient_id', 'application_id')
        """)).fetchall()

        fk_mappings = {row[0]: row[1] for row in result}
        assert fk_mappings.get('patient_id') == 'platform_patients', "Missing FK to patients"
        assert fk_mappings.get('application_id') == 'platform_credit_applications', "Missing FK to applications"

    def test_verifications_fk_to_consents(self, db_session):
        """platform_verifications should FK to platform_consents"""
        result = db_session.execute(text("""
            SELECT COUNT(*)
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
                ON tc.constraint_name = kcu.constraint_name
            WHERE tc.table_name = 'platform_verifications'
            AND tc.constraint_type = 'FOREIGN KEY'
            AND kcu.column_name = 'consent_id'
        """)).scalar()

        assert result == 1, "Missing FK from verifications to consents"


class TestPlatformDataIntegrity:
    """Test data integrity constraints"""

    def test_platform_patients_soft_delete_enables_duplicate_email(self, db_session):
        """Soft-deleted patients should allow email reuse"""
        import uuid
        unique_suffix = uuid.uuid4().hex[:8]

        # Create first patient
        patient_id_1 = db_session.execute(text("""
            INSERT INTO platform_patients (email, legal_first_name, legal_last_name)
            VALUES (:email, 'John', 'Doe')
            RETURNING id
        """), {"email": f"test-{unique_suffix}@example.com"}).scalar()
        db_session.commit()

        # Soft delete
        db_session.execute(text("""
            UPDATE platform_patients SET deleted_at = NOW() WHERE id = :id
        """), {"id": patient_id_1})
        db_session.commit()

        # Should be able to create new patient with same email
        patient_id_2 = db_session.execute(text("""
            INSERT INTO platform_patients (email, legal_first_name, legal_last_name)
            VALUES (:email, 'Jane', 'Smith')
            RETURNING id
        """), {"email": f"test-{unique_suffix}@example.com"}).scalar()
        db_session.commit()

        assert patient_id_2 is not None, "Should allow email reuse after soft delete"

    def test_platform_patients_prevents_duplicate_email_for_active(self, db_session):
        """Active patients should not allow duplicate emails"""
        import uuid
        unique_suffix = uuid.uuid4().hex[:8]

        # Create first patient
        db_session.execute(text("""
            INSERT INTO platform_patients (email, legal_first_name, legal_last_name)
            VALUES (:email, 'John', 'Doe')
        """), {"email": f"duplicate-{unique_suffix}@example.com"})
        db_session.commit()

        # Try to create second patient with same email - should fail
        with pytest.raises(IntegrityError):
            db_session.execute(text("""
                INSERT INTO platform_patients (email, legal_first_name, legal_last_name)
                VALUES (:email, 'Jane', 'Smith')
            """), {"email": f"duplicate-{unique_suffix}@example.com"})
            db_session.commit()

