"""Clinic membership: the user -> clinic (vendor) link for the clinic API.

Per spec §13, clinics ARE the existing ``vendors`` table. There was no
user->vendor link in the schema before this (``app.core.auth`` carried only a
TODO stub), so this table establishes it. A row grants the referenced user
access to the clinic console scoped to ``vendor_id``.

The migration for ``platform_clinic_memberships`` is written separately
(migration 028); this model must match that DDL.
"""
from uuid import uuid4

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.db.base import Base


class PlatformClinicMembership(Base):
    """Links a ``users`` row to a ``vendors`` (clinic) row for clinic-API scoping."""

    __tablename__ = "platform_clinic_memberships"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    vendor_id = Column(
        UUID(as_uuid=True),
        ForeignKey("vendors.id", ondelete="CASCADE"),
        nullable=False,
    )
    role = Column(String(50), nullable=False, default="staff")

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("idx_platform_clinic_memberships_user", "user_id"),
        Index("idx_platform_clinic_memberships_vendor", "vendor_id"),
        Index(
            "idx_platform_clinic_memberships_user_vendor",
            "user_id",
            "vendor_id",
            unique=True,
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<PlatformClinicMembership(user_id={self.user_id}, "
            f"vendor_id={self.vendor_id}, role={self.role})>"
        )
