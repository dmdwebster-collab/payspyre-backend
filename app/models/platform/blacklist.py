"""Blacklist entries (WS-I, migration 063) — Turnkey Tools → Blacklists parity.

Staff-maintained lists of SUSPICIOUS values (phone / email / name / SIN /
driver's license / bank account number). A match at application intake FLAGS
the file and routes an auto-approve to MANUAL REVIEW — it NEVER auto-declines
(Dave's rule: mismatch/suspicion goes to a human, not to an automatic reject).

Rows are never hard-deleted: removing one flips ``active`` off (auditable),
mirroring the custom-transaction convention. ``value_normalized`` is the
match key (lowercased, digits-only for phone/account, etc. — see
``app.services.blacklists.normalize_value``).

SIN caution: the SIN category stores the staff-entered watch value hashed the
same way it is normalized-then-compared — we deliberately do NOT keep a raw
plaintext SIN column here beyond the normalized digits the screener needs;
access is admin-gated.
"""
from uuid import uuid4

from sqlalchemy import Boolean, Column, DateTime, Index, String, func, text
from sqlalchemy.dialects.postgresql import ENUM, UUID

from app.db.base import Base


class PlatformBlacklistEntry(Base):
    __tablename__ = "platform_blacklist_entries"
    __table_args__ = (
        Index(
            "ix_platform_blacklist_cat_value_active",
            "category",
            "value_normalized",
            postgresql_where=text("active"),
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)

    category = Column(
        ENUM(
            "name",
            "sin",
            "phone",
            "email",
            "drivers_license",
            "account_number",
            name="platform_blacklist_category",
            create_type=False,
        ),
        nullable=False,
    )
    # As entered by staff (display).
    value = Column(String, nullable=False)
    # Canonical match key (see blacklists.normalize_value).
    value_normalized = Column(String, nullable=False)

    # MANDATORY: why this value is on the list (Turnkey shows it in the list).
    reason = Column(String, nullable=False)

    active = Column(Boolean, nullable=False, default=True, server_default=text("true"))

    created_by = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    deactivated_by = Column(String, nullable=True)
    deactivated_at = Column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<PlatformBlacklistEntry(category={self.category}, "
            f"value={self.value!r}, active={self.active})>"
        )
