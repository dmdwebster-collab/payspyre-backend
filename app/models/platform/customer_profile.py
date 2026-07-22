"""Customer Profile — the persistent, reusable applicant entity.

Dave (2026-07-21): *"the standard credit application gathers all the required
information. **This becomes the users profile information.** This user profile
information is **then attached to the requested finance terms and scored** to
produce an actual credit application."*

So: ONE profile -> MANY applications. Before this module the model was inverted —
``PlatformPatient`` was thin identity and the rich applicant data lived on each
``PlatformCreditApplication``, so re-applying meant re-entering everything.

DESIGN
------
``platform_customer_profiles`` — one row per patient (1:1, unique
``patient_id``). It carries no field values: only lifecycle (version counter,
lock, soft-delete). We EXTEND ``PlatformPatient`` rather than create a rival
identity table, because the patient id is referenced by applications, loans,
consents, events, verifications and messages.

``platform_customer_profile_fields`` — the values, one row per
``(profile, block, block_index, field_key)`` **version**. The key space is
exactly :mod:`app.services.customer_profile_schema`: nothing is stored that the
registry does not define.

WHY KEY/VALUE AND NOT 90 COLUMNS
--------------------------------
1. Dave's spec repeats whole blocks (Previous Address, Additional Income 1 & 2,
   and the Originations review wants several bank accounts). Rows model repeats;
   columns would mean triplicated column sets.
2. His mandate is that edits NEVER overwrite — the prior value becomes "former".
   Per-field versioning is the natural shape for that, and
   ``PlatformPatientField`` already proves the pattern in this codebase
   (``is_current`` / ``superseded_at`` / ``superseded_by_id``).
3. The field set is spec-driven and still moving (ID verification, bank
   verification and bureau automation will change WHO fills fields, per Dave, but
   also the exact set). A registry + key/value store absorbs that without a
   migration per field.

The scored, structured columns on ``platform_credit_applications`` are untouched
— they remain the decision engine's input. The profile is the SOURCE those
columns are populated FROM, and each application freezes the profile version it
was decided on (``profile_version`` / ``profile_snapshot``).
"""
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Index,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from app.db.base import Base


class PlatformCustomerProfile(Base):
    """Lifecycle header for one customer's profile. Values live in the fields table."""

    __tablename__ = "platform_customer_profiles"
    __table_args__ = (
        UniqueConstraint("patient_id", name="uq_customer_profile_patient"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    patient_id = Column(
        UUID(as_uuid=True), ForeignKey("platform_patients.id"), nullable=False, index=True
    )

    #: bumped on every edit; the version a field row was written under is stamped
    #: on the row, so ``version`` is also the id of the CURRENT profile revision.
    version = Column(Integer, nullable=False, default=1)

    #: schema revision the profile was captured under (Dave's sheet version)
    schema_version = Column(String, nullable=False, default="1.0")

    #: Lock = no further edits without an explicit unlock. Permissioned + audited.
    locked_at = Column(DateTime(timezone=True), nullable=True)
    locked_by = Column(String, nullable=True)
    lock_reason = Column(String, nullable=True)

    #: Soft delete only (PIPEDA retention), mirroring platform_patients.deleted_at.
    deleted_at = Column(DateTime(timezone=True), nullable=True)
    deleted_by = Column(String, nullable=True)
    delete_reason = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_by = Column(String, nullable=True)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    updated_by = Column(String, nullable=True)

    patient = relationship("PlatformPatient", backref="customer_profile")
    fields = relationship(
        "PlatformCustomerProfileField",
        back_populates="profile",
        cascade="all, delete-orphan",
    )

    @property
    def is_locked(self) -> bool:
        return self.locked_at is not None

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def __repr__(self) -> str:
        # No PII: profiles are the PII store, so the repr carries ids only.
        return (
            f"<PlatformCustomerProfile(id={self.id}, version={self.version}, "
            f"locked={self.is_locked}, deleted={self.is_deleted})>"
        )


class PlatformCustomerProfileField(Base):
    """One versioned field value.

    Never updated in place: an edit marks the current row ``is_current=False``,
    stamps ``superseded_at``/``superseded_by_id`` and inserts a new current row.
    The full history is therefore the profile changelog Dave asked for
    (time + user stamped) with no separate audit table.
    """

    __tablename__ = "platform_customer_profile_fields"
    __table_args__ = (
        Index(
            "ix_customer_profile_fields_current",
            "profile_id",
            "block",
            "block_index",
            "field_key",
            "is_current",
        ),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    profile_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_customer_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    #: registry block name (``personal``, ``bank_details``, ...)
    block = Column(String, nullable=False)
    #: 0 for every singleton block; >0 only for repeatable blocks (bank accounts)
    block_index = Column(Integer, nullable=False, default=0)
    #: registry field key within the block
    field_key = Column(String, nullable=False)

    #: JSONB so dates, numbers, strings and booleans all round-trip untyped.
    #: Sensitive fields (SIN) store ONLY a masked remnant here — see the service.
    value = Column(JSONB, nullable=True)

    #: where the value came from: self_reported / staff / id_doc / bank_verification / bureau
    source = Column(String, nullable=False, default="self_reported")

    #: profile.version this row was written under
    profile_version = Column(Integer, nullable=False, default=1)

    is_current = Column(Boolean, nullable=False, default=True)
    superseded_at = Column(DateTime(timezone=True), nullable=True)
    superseded_by_id = Column(
        BigInteger, ForeignKey("platform_customer_profile_fields.id"), nullable=True
    )

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_by = Column(String, nullable=True)

    profile = relationship("PlatformCustomerProfile", back_populates="fields")
    superseded_by = relationship(
        "PlatformCustomerProfileField", remote_side=[id], post_update=True
    )

    def __repr__(self) -> str:
        # Deliberately no value: these rows ARE the PII.
        return (
            f"<PlatformCustomerProfileField(id={self.id}, block={self.block}"
            f"#{self.block_index}, key={self.field_key}, "
            f"v{self.profile_version}, current={self.is_current})>"
        )
