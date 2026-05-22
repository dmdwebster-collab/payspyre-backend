"""Tests for KYC orchestration schema (PR #1)

These tests validate the new KYC orchestration tables, indexes,
and constraints added in migration 014_add_kyc_orchestration_tables.py

Critical validations:
- kyc_events WORM constraint (no UPDATE/DELETE allowed)
- kyc_webhook_inbox UNIQUE constraint for idempotency
- All new columns present on existing tables
- New tables created correctly
"""
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from app.db.base import get_db


class TestKycSchema:
    """Test KYC orchestration schema is properly configured"""

    def test_kyc_sessions_has_new_columns(self, db):
        """kyc_sessions should have new columns per spec"""
        # Check new columns exist
        result = db.execute(text("""
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_name = 'kyc_sessions'
            AND column_name IN ('role', 'vendor_verification_url', 'vendor_decision',
                              'vendor_payload', 'risk_score', 'risk_flags')
            ORDER BY column_name
        """)).fetchall()

        column_names = [row[0] for row in result]
        assert 'role' in column_names, "kyc_sessions.role column missing"
        assert 'vendor_verification_url' in column_names, "kyc_sessions.vendor_verification_url column missing"
        assert 'vendor_decision' in column_names, "kyc_sessions.vendor_decision column missing"
        assert 'vendor_payload' in column_names, "kyc_sessions.vendor_payload column missing"
        assert 'risk_score' in column_names, "kyc_sessions.risk_score column missing"
        assert 'risk_flags' in column_names, "kyc_sessions.risk_flags column missing"

    def test_kyc_sessions_indexes(self, db):
        """kyc_sessions should have new indexes per spec"""
        result = db.execute(text("""
            SELECT indexname
            FROM pg_indexes
            WHERE tablename = 'kyc_sessions'
            AND indexname IN ('idx_kyc_sessions_loan_app_role',
                            'idx_kyc_sessions_borrower_created',
                            'idx_kyc_sessions_vendor_session_id')
            ORDER BY indexname
        """)).fetchall()

        index_names = [row[0] for row in result]
        assert 'idx_kyc_sessions_loan_app_role' in index_names, "Missing loan_app_role index"
        assert 'idx_kyc_sessions_borrower_created' in index_names, "Missing borrower_created index"
        assert 'idx_kyc_sessions_vendor_session_id' in index_names, "Missing vendor_session_id unique index"

    def test_kyc_events_has_new_columns(self, db):
        """kyc_events should have new columns per spec"""
        result = db.execute(text("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'kyc_events'
            AND column_name IN ('from_status', 'to_status', 'actor_type',
                              'actor_id', 'webhook_signature', 'ip_address')
            ORDER BY column_name
        """)).fetchall()

        column_names = [row[0] for row in result]
        assert 'from_status' in column_names, "kyc_events.from_status column missing"
        assert 'to_status' in column_names, "kyc_events.to_status column missing"
        assert 'actor_type' in column_names, "kyc_events.actor_type column missing"
        assert 'actor_id' in column_names, "kyc_events.actor_id column missing"
        assert 'webhook_signature' in column_names, "kyc_events.webhook_signature column missing"
        assert 'ip_address' in column_names, "kyc_events.ip_address column missing"

    def test_kyc_events_worm_constraint(self, db):
        """kyc_events should have WORM protection (UPDATE/DELETE blocked)"""
        # Create a test event
        db.execute(text("""
            INSERT INTO kyc_events (kyc_session_id, event_type, payload)
            VALUES (gen_random_uuid(), 'test_event', '{"test": true}')
        """))
        db.commit()

        # Try to UPDATE - should fail
        with pytest.raises(ProgrammingError, match="Cannot modify kyc_events"):
            db.execute(text("""
                UPDATE kyc_events SET payload = '{"modified": true}' WHERE event_type = 'test_event'
            """))
            db.commit()

        # Try to DELETE - should fail
        with pytest.raises(ProgrammingError, match="Cannot modify kyc_events"):
            db.execute(text("""
                DELETE FROM kyc_events WHERE event_type = 'test_event'
            """))
            db.commit()

        # Clean up (INSERT should still work)
        db.rollback()

    def test_kyc_webhook_inbox_table_exists(self, db):
        """kyc_webhook_inbox table should exist with correct schema"""
        result = db.execute(text("""
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_name = 'kyc_webhook_inbox'
            ORDER BY ordinal_position
        """)).fetchall()

        # Check all expected columns exist
        column_names = [row[0] for row in result]
        expected_columns = [
            'id', 'vendor', 'vendor_event_id', 'received_at', 'processed_at',
            'raw_body', 'signature_header', 'result', 'error_message'
        ]

        for col in expected_columns:
            assert col in column_names, f"kyc_webhook_inbox.{col} column missing"

    def test_kyc_webhook_inbox_unique_constraint(self, db):
        """kyc_webhook_inbox should have UNIQUE constraint on (vendor, vendor_event_id)"""
        # Insert a webhook
        db.execute(text("""
            INSERT INTO kyc_webhook_inbox (vendor, vendor_event_id, raw_body, signature_header, result)
            VALUES ('didit', 'evt_test123', '\\x00', 'sig123', 'accepted')
        """))
        db.commit()

        # Try to insert duplicate - should fail
        with pytest.raises(IntegrityError):
            db.execute(text("""
                INSERT INTO kyc_webhook_inbox (vendor, vendor_event_id, raw_body, signature_header, result)
                VALUES ('didit', 'evt_test123', '\\x00', 'sig123', 'duplicate')
            """))
            db.commit()

        db.rollback()

    def test_kyb_applications_table_exists(self, db):
        """kyb_applications table should exist with correct schema"""
        result = db.execute(text("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'kyb_applications'
            ORDER BY ordinal_position
        """)).fetchall()

        column_names = [row[0] for row in result]
        expected_columns = [
            'id', 'loan_application_id', 'business_legal_name', 'bn_cra',
            'incorporation_jurisdiction', 'articles_doc_id', 'void_cheque_doc_id',
            'signing_authority_doc_id', 'status', 'reviewer_id', 'decision_notes',
            'decided_at', 'created_at', 'updated_at'
        ]

        for col in expected_columns:
            assert col in column_names, f"kyb_applications.{col} column missing"

    def test_kyb_beneficial_owners_table_exists(self, db):
        """kyb_beneficial_owners table should exist with correct schema"""
        result = db.execute(text("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'kyb_beneficial_owners'
            ORDER BY ordinal_position
        """)).fetchall()

        column_names = [row[0] for row in result]
        expected_columns = [
            'id', 'kyb_application_id', 'full_name', 'ownership_pct',
            'is_control_person', 'kyc_session_id', 'created_at'
        ]

        for col in expected_columns:
            assert col in column_names, f"kyb_beneficial_owners.{col} column missing"

    def test_enum_types_exist(self, db):
        """New enum types should exist"""
        result = db.execute(text("""
            SELECT typname
            FROM pg_type
            WHERE typname IN ('kyc_role', 'kyc_vendor_decision', 'webhook_result', 'kyb_status')
            ORDER BY typname
        """)).fetchall()

        type_names = [row[0] for row in result]
        assert 'kyc_role' in type_names, "kyc_role enum type missing"
        assert 'kyc_vendor_decision' in type_names, "kyc_vendor_decision enum type missing"
        assert 'webhook_result' in type_names, "webhook_result enum type missing"
        assert 'kyb_status' in type_names, "kyb_status enum type missing"


class TestKycModels:
    """Test SQLAlchemy models match the database schema"""

    def test_kyc_session_model_attributes(self):
        """KycSession model should have all new attributes"""
        from app.models.kyc import KycSession

        # Check new attributes exist
        assert hasattr(KycSession, 'role'), "KycSession.role missing"
        assert hasattr(KycSession, 'vendor_verification_url'), "KycSession.vendor_verification_url missing"
        assert hasattr(KycSession, 'vendor_decision'), "KycSession.vendor_decision missing"
        assert hasattr(KycSession, 'vendor_payload'), "KycSession.vendor_payload missing"
        assert hasattr(KycSession, 'risk_score'), "KycSession.risk_score missing"
        assert hasattr(KycSession, 'risk_flags'), "KycSession.risk_flags missing"

    def test_kyc_event_model_attributes(self):
        """KycEvent model should have all new attributes"""
        from app.models.kyc import KycEvent

        assert hasattr(KycEvent, 'from_status'), "KycEvent.from_status missing"
        assert hasattr(KycEvent, 'to_status'), "KycEvent.to_status missing"
        assert hasattr(KycEvent, 'actor_type'), "KycEvent.actor_type missing"
        assert hasattr(KycEvent, 'actor_id'), "KycEvent.actor_id missing"
        assert hasattr(KycEvent, 'webhook_signature'), "KycEvent.webhook_signature missing"
        assert hasattr(KycEvent, 'ip_address'), "KycEvent.ip_address missing"

    def test_new_models_exist(self):
        """New model classes should exist"""
        from app.models import kyc

        assert hasattr(kyc, 'KycWebhookInbox'), "KycWebhookInbox model missing"
        assert hasattr(kyc, 'KybApplication'), "KybApplication model missing"
        assert hasattr(kyc, 'KybBeneficialOwner'), "KybBeneficialOwner model missing"
