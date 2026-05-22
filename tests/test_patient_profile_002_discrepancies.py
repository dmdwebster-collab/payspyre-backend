"""Integration tests for Patient Profile Service - Discrepancy Detection (PR P2)

Tests discrepancy detection between self_reported and practice_prefill values.
Validates per-spec Â§8.4: discrepancies detected when same field has different
values from different sources.

Critical validations:
- Discrepancy detection finds conflicting values across sources
- Discrepancy endpoint returns all conflicts
- Same value from different sources is NOT a discrepancy
- get_discrepancies() returns structured discrepancy objects
- Field updates trigger automatic discrepancy detection
"""
import pytest
from sqlalchemy.orm import Session
from datetime import datetime, date
from uuid import uuid4

from app.services.patient_profile import PatientProfileService
from app.schemas.patient import PatientFieldUpdateRequest
from app.models.platform.patient import PlatformPatient
from app.models.platform.patient_field import PlatformPatientField


class TestDiscrepancyDetection:
    """Test discrepancy detection logic"""

    def test_no_discrepancies_with_single_source(self, db_session: Session):
        """Single source values should not create discrepancies"""
        service = PatientProfileService(db_session)
        patient_id = _create_test_patient(db_session)

        # Write multiple fields from same source
        service._write_field(
            patient_id=patient_id,
            field_key="legal_first_name",
            value="John",
            source="self_reported",
        )
        service._write_field(
            patient_id=patient_id,
            field_key="email",
            value="john@example.com",
            source="self_reported",
        )

        discrepancies = service.detect_discrepancies(patient_id)
        assert len(discrepancies) == 0

    def test_discrepancy_detected_different_values_different_sources(self, db_session: Session):
        """Different values from different sources should create discrepancy"""
        service = PatientProfileService(db_session)
        patient_id = _create_test_patient(db_session)

        # Write same field from different sources with different values
        service._write_field(
            patient_id=patient_id,
            field_key="legal_first_name",
            value="John",
            source="self_reported",
        )
        service._write_field(
            patient_id=patient_id,
            field_key="legal_first_name",
            value="Jon",
            source="practice_prefill",
        )

        discrepancies = service.detect_discrepancies(patient_id)
        assert len(discrepancies) == 1
        assert discrepancies[0].field_key == "legal_first_name"
        assert discrepancies[0].values["self_reported"] == "John"
        assert discrepancies[0].values["practice_prefill"] == "Jon"

    def test_same_value_different_sources_not_discrepancy(self, db_session: Session):
        """Same value from different sources should NOT be a discrepancy"""
        service = PatientProfileService(db_session)
        patient_id = _create_test_patient(db_session)

        # Write same field from different sources with SAME value
        service._write_field(
            patient_id=patient_id,
            field_key="legal_first_name",
            value="John",
            source="self_reported",
        )
        service._write_field(
            patient_id=patient_id,
            field_key="legal_first_name",
            value="John",
            source="practice_prefill",
        )

        discrepancies = service.detect_discrepancies(patient_id)
        assert len(discrepancies) == 0

    def test_multiple_discrepancies_detected(self, db_session: Session):
        """Multiple fields with conflicts should all be detected"""
        service = PatientProfileService(db_session)
        patient_id = _create_test_patient(db_session)

        # Create discrepancies for 3 fields
        service._write_field(
            patient_id=patient_id,
            field_key="legal_first_name",
            value="John",
            source="self_reported",
        )
        service._write_field(
            patient_id=patient_id,
            field_key="legal_first_name",
            value="Jon",
            source="practice_prefill",
        )

        service._write_field(
            patient_id=patient_id,
            field_key="email",
            value="john@example.com",
            source="self_reported",
        )
        service._write_field(
            patient_id=patient_id,
            field_key="email",
            value="jon@example.com",
            source="practice_prefill",
        )

        service._write_field(
            patient_id=patient_id,
            field_key="phone_e164",
            value="+16045551234",
            source="self_reported",
        )
        service._write_field(
            patient_id=patient_id,
            field_key="phone_e164",
            value="+16045559999",
            source="id_doc",
        )

        discrepancies = service.detect_discrepancies(patient_id)
        assert len(discrepancies) == 3

        field_keys = {d.field_key for d in discrepancies}
        assert "legal_first_name" in field_keys
        assert "email" in field_keys
        assert "phone_e164" in field_keys

    def test_three_sources_with_two_conflicting(self, db_session: Session):
        """Three sources where two agree, one differs -> discrepancy detected"""
        service = PatientProfileService(db_session)
        patient_id = _create_test_patient(db_session)

        # self_reported: John
        service._write_field(
            patient_id=patient_id,
            field_key="legal_first_name",
            value="John",
            source="self_reported",
        )
        # practice_prefill: John (agrees with self_reported)
        service._write_field(
            patient_id=patient_id,
            field_key="legal_first_name",
            value="John",
            source="practice_prefill",
        )
        # id_doc: Jon (conflicts!)
        service._write_field(
            patient_id=patient_id,
            field_key="legal_first_name",
            value="Jon",
            source="id_doc",
        )

        discrepancies = service.detect_discrepancies(patient_id)
        assert len(discrepancies) == 1
        assert discrepancies[0].field_key == "legal_first_name"
        # Should have all 3 sources in values
        assert len(discrepancies[0].values) == 3
        assert discrepancies[0].values["self_reported"] == "John"
        assert discrepancies[0].values["practice_prefill"] == "John"
        assert discrepancies[0].values["id_doc"] == "Jon"

    def test_superseded_values_not_in_discrepancy_check(self, db_session: Session):
        """Superseded values should NOT participate in discrepancy detection"""
        service = PatientProfileService(db_session)
        patient_id = _create_test_patient(db_session)

        # Write from self_reported
        field1 = service._write_field(
            patient_id=patient_id,
            field_key="legal_first_name",
            value="John",
            source="self_reported",
        )

        # Supersede with new value from self_reported
        service._write_field(
            patient_id=patient_id,
            field_key="legal_first_name",
            value="Johnny",
            source="self_reported",
        )

        # Write from practice_prefill (same as superseded value)
        service._write_field(
            patient_id=patient_id,
            field_key="legal_first_name",
            value="John",
            source="practice_prefill",
        )

        # Current values: self_reported="Johnny", practice_prefill="John"
        # Superseded value "John" from self_reported should NOT count
        discrepancies = service.detect_discrepancies(patient_id)
        assert len(discrepancies) == 1
        # Only current values should be in discrepancy
        assert discrepancies[0].values["self_reported"] == "Johnny"
        assert discrepancies[0].values["practice_prefill"] == "John"


class TestDiscrepancyResponseStructure:
    """Test discrepancy response structure per schema"""

    def test_discrepancy_has_detected_at_timestamp(self, db_session: Session):
        """Discrepancy objects should include detected_at timestamp"""
        service = PatientProfileService(db_session)
        patient_id = _create_test_patient(db_session)

        service._write_field(
            patient_id=patient_id,
            field_key="email",
            value="a@example.com",
            source="self_reported",
        )
        service._write_field(
            patient_id=patient_id,
            field_key="email",
            value="b@example.com",
            source="practice_prefill",
        )

        discrepancies = service.detect_discrepancies(patient_id)
        assert len(discrepancies) == 1
        assert discrepancies[0].detected_at is not None
        assert isinstance(discrepancies[0].detected_at, datetime)

    def test_discrepancy_values_dict_source_to_value(self, db_session: Session):
        """Discrepancy.values should be dict mapping source -> value"""
        service = PatientProfileService(db_session)
        patient_id = _create_test_patient(db_session)

        service._write_field(
            patient_id=patient_id,
            field_key="phone_e164",
            value="+1111",
            source="self_reported",
        )
        service._write_field(
            patient_id=patient_id,
            field_key="phone_e164",
            value="+2222",
            source="id_doc",
        )
        service._write_field(
            patient_id=patient_id,
            field_key="phone_e164",
            value="+3333",
            source="bureau",
        )

        discrepancies = service.detect_discrepancies(patient_id)
        assert len(discrepancies) == 1

        values = discrepancies[0].values
        assert isinstance(values, dict)
        assert values["self_reported"] == "+1111"
        assert values["id_doc"] == "+2222"
        assert values["bureau"] == "+3333"


class TestDiscrepancyEventLogging:
    """Test that discrepancies are logged as events"""

    def test_discrepancy_logged_on_field_update(self, db_session: Session):
        """Field update with conflicting value should log discrepancy event"""
        service = PatientProfileService(db_session)
        patient_id = _create_test_patient(db_session)

        # Set initial value
        service.update_patient_field(
            patient_id=patient_id,
            data=PatientFieldUpdateRequest(
                field_key="legal_last_name",
                value="Smith",
                source="self_reported",
            ),
        )

        # Update with conflicting value from different source
        service.update_patient_field(
            patient_id=patient_id,
            data=PatientFieldUpdateRequest(
                field_key="legal_last_name",
                value="Smyth",
                source="practice_prefill",
            ),
        )

        # Should have logged discrepancy event
        from sqlalchemy import text
        result = db_session.execute(text("""
            SELECT event_type, payload
            FROM platform_events
            WHERE patient_id = :patient_id
            AND event_type = 'patient.field_discrepancy_detected'
            ORDER BY occurred_at DESC
            LIMIT 1
        """), {"patient_id": patient_id}).fetchone()

        assert result is not None
        payload = result[1]
        assert payload["field_key"] == "legal_last_name"
        assert "existing_value" in payload
        assert "new_value" in payload


class TestGetPatientProfileWithDiscrepancies:
    """Test get_patient_profile includes discrepancy info"""

    def test_get_profile_returns_current_fields(self, db_session: Session):
        """get_patient_profile should return current fields from all sources"""
        service = PatientProfileService(db_session)
        patient_id = _create_test_patient(db_session)

        service._write_field(
            patient_id=patient_id,
            field_key="email",
            value="a@example.com",
            source="self_reported",
        )
        service._write_field(
            patient_id=patient_id,
            field_key="email",
            value="b@example.com",
            source="practice_prefill",
        )

        profile = service.get_patient_profile(patient_id)
        assert profile["patient"] is not None
        assert len(profile["fields"]) == 2

        field_keys_with_sources = [
            f"{f.field_key}:{f.source}" for f in profile["fields"]
        ]
        assert "email:self_reported" in field_keys_with_sources
        assert "email:practice_prefill" in field_keys_with_sources


# Helper
def _create_test_patient(db_session: Session) -> uuid4:
    """Create a test patient and return ID"""
    import uuid
    patient = PlatformPatient(
        legal_first_name="Test",
        legal_last_name="Patient",
        email=f"test-{uuid.uuid4().hex[:8]}@example.com",
    )
    db_session.add(patient)
    db_session.commit()
    db_session.refresh(patient)
    return patient.id




