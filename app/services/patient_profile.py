from datetime import datetime
from typing import Optional, Any
from uuid import UUID

from sqlalchemy.orm import Session
from sqlalchemy import and_

from app.models.platform.patient import PlatformPatient
from app.models.platform.patient_field import PlatformPatientField
from app.models.platform.event import PlatformEvent
from app.schemas.patient import (
    PatientQuickstartRequest,
    PatientFieldUpdateRequest,
    PatientFieldValue,
    FieldDiscrepancy,
)


class PatientProfileService:
    """Service for patient profile CRUD with source-tagged field writes."""

    FIELD_SOURCES = ["self_reported", "practice_prefill", "id_doc", "bureau"]

    def __init__(self, db: Session):
        self.db = db

    def create_patient_quickstart(self, data: PatientQuickstartRequest) -> PlatformPatient:
        """
        Create a patient with self-reported name + email (required).
        Minimum viable profile for quick-start flow.
        """
        patient = PlatformPatient(
            legal_first_name=data.legal_first_name,
            legal_last_name=data.legal_last_name,
            email=data.email,
            phone_e164=data.phone_e164,
            dob=data.dob,
            verification_depth="email_verified",  # Will be set to 'none' until email verified
        )
        self.db.add(patient)
        self.db.commit()
        self.db.refresh(patient)

        # Write source-tagged fields for quickstart data
        self._write_field(
            patient_id=patient.id,
            field_key="legal_first_name",
            value=data.legal_first_name,
            source="self_reported",
        )
        self._write_field(
            patient_id=patient.id,
            field_key="legal_last_name",
            value=data.legal_last_name,
            source="self_reported",
        )
        self._write_field(
            patient_id=patient.id,
            field_key="email",
            value=data.email,
            source="self_reported",
        )
        if data.phone_e164:
            self._write_field(
                patient_id=patient.id,
                field_key="phone_e164",
                value=data.phone_e164,
                source="self_reported",
            )
        if data.dob:
            self._write_field(
                patient_id=patient.id,
                field_key="dob",
                value=data.dob.isoformat(),
                source="self_reported",
            )

        # Log quickstart event
        self._log_event(
            patient_id=patient.id,
            event_type="patient.quickstart_completed",
            actor="patient",
            payload={"email": data.email, "source": "self_reported"},
        )

        return patient

    def update_patient_field(
        self,
        patient_id: UUID,
        data: PatientFieldUpdateRequest,
        actor: str = "system",
    ) -> PlatformPatientField:
        """
        Update a patient field with source tagging.
        Handles supersession of previous current values.
        """
        patient = self.db.query(PlatformPatient).filter(PlatformPatient.id == patient_id).first()
        if not patient:
            raise ValueError(f"Patient {patient_id} not found")

        # Check for discrepancy before updating
        existing_current = (
            self.db.query(PlatformPatientField)
            .filter(
                and_(
                    PlatformPatientField.patient_id == patient_id,
                    PlatformPatientField.field_key == data.field_key,
                    PlatformPatientField.is_current == True,
                )
            )
            .first()
        )

        new_field = self._write_field(
            patient_id=patient_id,
            field_key=data.field_key,
            value=data.value,
            source=data.source,
            confidence=data.confidence,
            actor=actor,
        )

        # If there was a previous current value with different value from different source, log discrepancy
        if existing_current and existing_current.source != data.source:
            existing_value = existing_current.field_value
            new_value = data.value
            if existing_value != new_value:
                self._log_discrepancy_event(
                    patient_id=patient_id,
                    field_key=data.field_key,
                    existing_value=existing_value,
                    existing_source=existing_current.source,
                    new_value=new_value,
                    new_source=data.source,
                    actor=actor,
                )

        return new_field

    def get_patient_profile(self, patient_id: UUID) -> dict:
        """
        Get patient profile with current source-tagged fields.
        """
        patient = self.db.query(PlatformPatient).filter(PlatformPatient.id == patient_id).first()
        if not patient:
            raise ValueError(f"Patient {patient_id} not found")

        current_fields = (
            self.db.query(PlatformPatientField)
            .filter(
                and_(
                    PlatformPatientField.patient_id == patient_id,
                    PlatformPatientField.is_current == True,
                )
            )
            .all()
        )

        return {
            "patient": patient,
            "fields": [PatientFieldValue.model_validate(f) for f in current_fields],
        }

    def detect_discrepancies(self, patient_id: UUID) -> list[FieldDiscrepancy]:
        """
        Find fields with conflicting values from different sources.
        Returns list of discrepancies with detected_at timestamp.
        """
        discrepancies = []

        # Group all current fields by field_key
        current_fields = (
            self.db.query(PlatformPatientField)
            .filter(
                and_(
                    PlatformPatientField.patient_id == patient_id,
                    PlatformPatientField.is_current == True,
                )
            )
            .all()
        )

        field_groups: dict[str, list[PlatformPatientField]] = {}
        for field in current_fields:
            if field.field_key not in field_groups:
                field_groups[field.field_key] = []
            field_groups[field.field_key].append(field)

        # Check for fields with multiple sources having different values
        for field_key, fields in field_groups.items():
            if len(fields) > 1:
                # Multiple sources for same field - check for value mismatch
                value_sources: dict[str, Any] = {}
                for field in fields:
                    value_sources[field.source] = field.field_value

                # If sources have different values, it's a discrepancy
                unique_values = set(str(v) for v in value_sources.values())
                if len(unique_values) > 1:
                    discrepancies.append(
                        FieldDiscrepancy(
                            field_key=field_key,
                            values=value_sources,
                            detected_at=datetime.utcnow(),
                        )
                    )

        return discrepancies

    def get_field_history(self, patient_id: UUID, field_key: str) -> list[PatientFieldValue]:
        """
        Get full history of a field including superseded values.
        Ordered by verified_at DESC (newest first).
        """
        fields = (
            self.db.query(PlatformPatientField)
            .filter(
                and_(
                    PlatformPatientField.patient_id == patient_id,
                    PlatformPatientField.field_key == field_key,
                )
            )
            .order_by(PlatformPatientField.verified_at.desc())
            .all()
        )

        return [PatientFieldValue.model_validate(f) for f in fields]

    def _write_field(
        self,
        patient_id: UUID,
        field_key: str,
        value: Any,
        source: str,
        confidence: Optional[float] = None,
        actor: str = "system",
    ) -> PlatformPatientField:
        """
        Write a field value with source tagging.
        Supersedes any existing current value from the same source.
        """
        if source not in self.FIELD_SOURCES:
            raise ValueError(f"Invalid source: {source}. Must be one of {self.FIELD_SOURCES}")

        # Find existing current field with same source and key
        existing_current = (
            self.db.query(PlatformPatientField)
            .filter(
                and_(
                    PlatformPatientField.patient_id == patient_id,
                    PlatformPatientField.field_key == field_key,
                    PlatformPatientField.source == source,
                    PlatformPatientField.is_current == True,
                )
            )
            .first()
        )

        if existing_current:
            # Supersede the old value
            existing_current.is_current = False
            existing_current.superseded_at = datetime.utcnow()
            self.db.flush()

        # Create new field record
        new_field = PlatformPatientField(
            patient_id=patient_id,
            field_key=field_key,
            field_value=value,
            source=source,
            confidence=confidence,
            verified_at=datetime.utcnow(),
            is_current=True,
        )

        self.db.add(new_field)
        self.db.commit()
        self.db.refresh(new_field)

        # Link superseded record: old field points to new field
        if existing_current:
            existing_current.superseded_by_id = new_field.id
            self.db.commit()

        return new_field

    def _log_event(
        self,
        patient_id: UUID,
        event_type: str,
        actor: str,
        payload: dict,
    ) -> PlatformEvent:
        """Log an event to platform_events (append-only)."""
        event = PlatformEvent(
            patient_id=patient_id,
            event_type=event_type,
            actor=actor,
            payload=payload,
        )
        self.db.add(event)
        self.db.commit()
        return event

    def _log_discrepancy_event(
        self,
        patient_id: UUID,
        field_key: str,
        existing_value: Any,
        existing_source: str,
        new_value: Any,
        new_source: str,
        actor: str,
    ) -> PlatformEvent:
        """Log a field discrepancy detection event."""
        event = PlatformEvent(
            patient_id=patient_id,
            event_type="patient.field_discrepancy_detected",
            actor=actor,
            payload={
                "field_key": field_key,
                "existing_value": existing_value,
                "existing_source": existing_source,
                "new_value": new_value,
                "new_source": new_source,
            },
        )
        self.db.add(event)
        self.db.commit()
        return event
