from datetime import datetime
from uuid import UUID, uuid4
from typing import Optional, Any

from sqlalchemy import Column, DateTime, String, Numeric, BigInteger, Boolean, func, Text, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship

from app.db.base import Base


class PlatformPatientField(Base):
    __tablename__ = "platform_patient_fields"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    patient_id = Column(UUID(as_uuid=True), ForeignKey("platform_patients.id"), nullable=False)
    field_key = Column(String, nullable=False)
    field_value = Column(JSONB, nullable=False)
    source = Column(String, nullable=False)
    source_event_id = Column(BigInteger, nullable=True)
    confidence = Column(Numeric(3, 2), nullable=True)  # 0.00-1.00
    verified_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    is_current = Column(Boolean, nullable=False, default=True)
    superseded_at = Column(DateTime(timezone=True), nullable=True)
    superseded_by_id = Column(BigInteger, ForeignKey("platform_patient_fields.id"), nullable=True)

    # Relationships
    patient = relationship("PlatformPatient", back_populates="fields")
    superseded_by = relationship("PlatformPatientField", remote_side=[id], post_update=True)

    def __repr__(self) -> str:
        return f"<PlatformPatientField(id={self.id}, field_key={self.field_key}, source={self.source}, is_current={self.is_current})>"
