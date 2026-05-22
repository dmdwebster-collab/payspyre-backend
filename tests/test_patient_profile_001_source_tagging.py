"""Integration tests for Patient Profile Service - Source Tagging (PR P2)

Tests source-tagged field writes, supersession, and immutable history.
Validates platform_patient_fields behavior per spec Â§2.2.

Critical validations:
- Fields are written with correct source tagging
- Supersession works: new value from same source marks old as is_current=false
- Immutable history: all values preserved in platform_patient_fields
- Quickstart creates patient + initial fields with source=self_reported
- Field updates from different sources create parallel current values
"""
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from datetime import datetime, date
from uuid import uuid4

from app.services.patient_profile import PatientProfileService
from app.schemas.patient import PatientQuickstartRequest, PatientFieldUpdateRequest
from app.models.platform.patient import PlatformPatient
from app.models.platform.patient_field import PlatformPatientField


class TestPatientQuickstart:
    """Test quick-start patient creation with self-reported fields"""

    def test_quickstart_creates_patient_and_fields(self, db_session: Session):
        """Quickstart should create patient record + source-tagged fields"""
        service = PatientProfileService(db_session)
        import uuid
        unique_suffix = uuid.uuid4().hex[:8]
        data = PatientQuickstartRequest(
            legal_first_name="John",
            legal_last_name="Doe",
            email=f"john.doe.{unique_suffix}@example.com",
            phone_e164=f"+1604555{unique_suffix}",
            dob=date(1990, 5, 15),
        )

        patient = service.create_patient_quickstart(data)

        # Verify patient created
        assert patient.id is not None
        assert patient.legal_first_name == "John"
        assert patient.email.startswith("john.doe.")
        assert patient.email.endswith("@example.com")

        # Verify source-tagged fields created
        fields = db_session.query(PlatformPatientField).filter(
            PlatformPatientField.patient_id == patient.id,
            PlatformPatientField.is_current == True,
        ).all()

        field_keys = {f.field_key for f in fields}
        assert "legal_first_name" in field_keys
        assert "legal_last_name" in field_keys
        assert "email" in field_keys
        assert "phone_e164" in field_keys
        assert "dob" in field_keys

        # All should be self_reported source
        for field in fields:
            assert field.source == "self_reported"

    def test_quickstart_logs_event(self, db_session: Session):
        """Quickstart should log patient.quickstart_completed event"""
        service = PatientProfileService(db_session)
        import uuid
        unique_suffix = uuid.uuid4().hex[:8]
        data = PatientQuickstartRequest(
            legal_first_name="Jane",
            legal_last_name="Smith",
            email=f"jane.smith.{unique_suffix}@example.com",
        )

        patient = service.create_patient_quickstart(data)

        # Check for quickstart event
        result = db_session.execute(text("""
            SELECT event_type, actor, payload
            FROM platform_events
            WHERE patient_id = :patient_id
            AND event_type = 'patient.quickstart_completed'
        """), {"patient_id": patient.id}).fetchone()

        assert result is not None
        assert result[0] == "patient.quickstart_completed"
        assert result[1] == "patient"
        assert result[2]["email"].startswith("jane.smith.")
        assert result[2]["email"].endswith("@example.com")


class TestSourceTaggedFieldWrites:
    """Test source-tagged field writes with supersession"""

    def test_write_field_creates_record(self, db_session: Session):
        """Writing a field should create platform_patient_field record"""
        service = PatientProfileService(db_session)
        patient_id = _create_test_patient(db_session)

        field = service._write_field(
            patient_id=patient_id,
            field_key="email",
            value="newemail@example.com",
            source="practice_prefill",
        )

        assert field.id is not None
        assert field.patient_id == patient_id
        assert field.field_key == "email"
        assert field.field_value == "newemail@example.com"
        assert field.source == "practice_prefill"
        assert field.is_current is True

    def test_write_field_supersedes_same_source(self, db_session: Session):
        """Writing same field from same source should supersede old value"""
        service = PatientProfileService(db_session)
        patient_id = _create_test_patient(db_session)

        # Write initial value
        field1 = service._write_field(
            patient_id=patient_id,
            field_key="email",
            value="email1@example.com",
            source="self_reported",
        )

        # Write new value from same source
        field2 = service._write_field(
            patient_id=patient_id,
            field_key="email",
            value="email2@example.com",
            source="self_reported",
        )

        # First field should be superseded
        db_session.refresh(field1)
        assert field1.is_current is False
        assert field1.superseded_at is not None
        assert field1.superseded_by_id == field2.id

        # Second field should be current
        assert field2.is_current is True

    def test_write_field_different_sources_parallel(self, db_session: Session):
        """Writing same field from different sources creates parallel current values"""
        service = PatientProfileService(db_session)
        patient_id = _create_test_patient(db_session)

        # Write from practice_prefill
        field1 = service._write_field(
            patient_id=patient_id,
            field_key="legal_first_name",
            value="PracticeName",
            source="practice_prefill",
        )

        # Write from self_reported (different value)
        field2 = service._write_field(
            patient_id=patient_id,
            field_key="legal_first_name",
            value="SelfName",
            source="self_reported",
        )

        # Both should be current (different sources)
        db_session.refresh(field1)
        db_session.refresh(field2)
        assert field1.is_current is True
        assert field2.is_current is True

        # Query current fields for this key should return 2
        current_fields = db_session.query(PlatformPatientField).filter(
            PlatformPatientField.patient_id == patient_id,
            PlatformPatientField.field_key == "legal_first_name",
            PlatformPatientField.is_current == True,
        ).all()
        assert len(current_fields) == 2

    def test_invalid_source_raises_error(self, db_session: Session):
        """Writing with invalid source should raise ValueError"""
        service = PatientProfileService(db_session)
        patient_id = _create_test_patient(db_session)

        with pytest.raises(ValueError, match="Invalid source"):
            service._write_field(
                patient_id=patient_id,
                field_key="email",
                value="test@example.com",
                source="invalid_source",
            )


class TestImmutableHistory:
    """Test that field history is immutable and preserved"""

    def test_field_history_preserved(self, db_session: Session):
        """Field history should preserve all values including superseded"""
        service = PatientProfileService(db_session)
        patient_id = _create_test_patient(db_session)

        # Write 3 versions of same field
        service._write_field(
            patient_id=patient_id,
            field_key="email",
            value="v1@example.com",
            source="self_reported",
        )
        service._write_field(
            patient_id=patient_id,
            field_key="email",
            value="v2@example.com",
            source="self_reported",
        )
        service._write_field(
            patient_id=patient_id,
            field_key="email",
            value="v3@example.com",
            source="self_reported",
        )

        # History should have 3 records
        history = service.get_field_history(patient_id, "email")
        assert len(history) == 3

        # Should be ordered newest first
        assert history[0].field_value == "v3@example.com"
        assert history[1].field_value == "v2@example.com"
        assert history[2].field_value == "v1@example.com"

    def test_superseded_fields_have_timestamps(self, db_session: Session):
        """Superseded fields should have superseded_at timestamp"""
        service = PatientProfileService(db_session)
        patient_id = _create_test_patient(db_session)

        field1 = service._write_field(
            patient_id=patient_id,
            field_key="email",
            value="v1@example.com",
            source="self_reported",
        )

        # Supersede it
        field2 = service._write_field(
            patient_id=patient_id,
            field_key="email",
            value="v2@example.com",
            source="self_reported",
        )

        db_session.refresh(field1)
        assert field1.superseded_at is not None
        # Compare aware datetimes (both are timezone-aware from Postgres)
        from datetime import timezone
        assert field1.superseded_at <= datetime.now(timezone.utc)


class TestUpdatePatientField:
    """Test update_patient_field API method"""

    def test_update_field_logs_discrepancy_on_mismatch(self, db_session: Session):
        """Updating field from different source with different value should log discrepancy"""
        service = PatientProfileService(db_session)
        patient_id = _create_test_patient(db_session)

        # Set initial value from self_reported
        service.update_patient_field(
            patient_id=patient_id,
            data=PatientFieldUpdateRequest(
                field_key="legal_first_name",
                value="John",
                source="self_reported",
            ),
        )

        # Update from practice_prefill with different value
        service.update_patient_field(
            patient_id=patient_id,
            data=PatientFieldUpdateRequest(
                field_key="legal_first_name",
                value="Jon",  # Different!
                source="practice_prefill",
            ),
        )

        # Check for discrepancy event
        result = db_session.execute(text("""
            SELECT event_type, payload
            FROM platform_events
            WHERE patient_id = :patient_id
            AND event_type = 'patient.field_discrepancy_detected'
        """), {"patient_id": patient_id}).fetchone()

        assert result is not None
        assert result[0] == "patient.field_discrepancy_detected"
        payload = result[1]
        assert payload["field_key"] == "legal_first_name"
        assert payload["existing_value"] == "John"
        assert payload["existing_source"] == "self_reported"
        assert payload["new_value"] == "Jon"
        assert payload["new_source"] == "practice_prefill"

    def test_update_field_no_discrepancy_on_same_value(self, db_session: Session):
        """Updating field with same value from different source should not log discrepancy"""
        service = PatientProfileService(db_session)
        patient_id = _create_test_patient(db_session)

        # Set initial value
        service.update_patient_field(
            patient_id=patient_id,
            data=PatientFieldUpdateRequest(
                field_key="legal_first_name",
                value="John",
                source="self_reported",
            ),
        )

        # Update from different source with SAME value
        service.update_patient_field(
            patient_id=patient_id,
            data=PatientFieldUpdateRequest(
                field_key="legal_first_name",
                value="John",
                source="practice_prefill",
            ),
        )

        # Should NOT have discrepancy event
        result = db_session.execute(text("""
            SELECT COUNT(*)
            FROM platform_events
            WHERE patient_id = :patient_id
            AND event_type = 'patient.field_discrepancy_detected'
        """), {"patient_id": patient_id}).fetchone()

        assert result[0] == 0

    def test_update_nonexistent_patient_raises_error(self, db_session: Session):
        """Updating field for non-existent patient should raise ValueError"""
        service = PatientProfileService(db_session)
        fake_id = uuid4()

        with pytest.raises(ValueError, match="not found"):
            service.update_patient_field(
                patient_id=fake_id,
                data=PatientFieldUpdateRequest(
                    field_key="email",
                    value="test@example.com",
                    source="self_reported",
                ),
            )


# Helper fixtures
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




