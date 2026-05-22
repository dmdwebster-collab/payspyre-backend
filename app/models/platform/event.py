from datetime import datetime
from uuid import UUID, uuid4
from typing import Optional, Any

from sqlalchemy import Column, DateTime, String, BigInteger, func, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship

from app.db.base import Base


class PlatformEvent(Base):
    __tablename__ = "platform_events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    patient_id = Column(UUID(as_uuid=True), ForeignKey("platform_patients.id"), nullable=True)
    application_id = Column(UUID(as_uuid=True), ForeignKey("platform_credit_applications.id"), nullable=True)
    event_type = Column(String, nullable=False)
    actor = Column(String, nullable=False)
    payload = Column(JSONB, nullable=False)
    correlation_id = Column(UUID(as_uuid=True), nullable=True)

    # Relationships
    patient = relationship("PlatformPatient", back_populates="events")
    application = relationship("PlatformCreditApplication", back_populates="events")

    def __repr__(self) -> str:
        return f"<PlatformEvent(id={self.id}, type={self.event_type}, occurred_at={self.occurred_at})>"
