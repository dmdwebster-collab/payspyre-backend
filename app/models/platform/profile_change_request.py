"""Vendor profile change-request model (spec §3.6).

DECISION (locked): vendors CANNOT edit their profile directly. The profile is
read-only; any change is submitted as an admin-routed change request that an
admin must approve out-of-band before it mutates the ``vendors`` row. This table
is the durable record of those pending requests — it does NOT mutate ``vendors``.

``requested_changes`` is a JSONB map of WHITELISTED vendor-editable keys only
(``contact_name``, ``email``, ``phone``, ``address_line1``, ``address_line2``,
``city``, ``province``, ``postal_code``). Never-requestable / admin-only fields
(``business_name``, ``dba_name``, ``business_type``, ``status``,
``compliance_score``, ``license_*``) are rejected at the API layer and must never
appear here. The whitelist is enforced server-side, not trusted from the client.
"""
from uuid import uuid4

from sqlalchemy import Column, DateTime, ForeignKey, Text, func
from sqlalchemy.dialects.postgresql import UUID, JSONB, ENUM
from sqlalchemy.orm import relationship

from app.db.base import Base


class PlatformVendorProfileChangeRequest(Base):
    __tablename__ = "platform_vendor_profile_change_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    vendor_id = Column(
        UUID(as_uuid=True),
        ForeignKey("vendors.id"),
        nullable=False,
    )

    # Whitelisted vendor-editable keys only (enforced at the API layer).
    requested_changes = Column(JSONB, nullable=False, default=lambda: {})

    status = Column(
        ENUM(
            "pending",
            "approved",
            "rejected",
            name="platform_vendor_profile_change_status",
            create_type=False,
        ),
        nullable=False,
        default="pending",
    )

    # Free-text reason supplied by the vendor (optional).
    note = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    vendor = relationship("Vendor")

    def __repr__(self) -> str:
        return (
            f"<PlatformVendorProfileChangeRequest(id={self.id}, "
            f"vendor_id={self.vendor_id}, status={self.status})>"
        )
