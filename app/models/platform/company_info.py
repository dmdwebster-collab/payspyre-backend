"""``platform_company_info`` — single-row company configuration (TL video 07 §2.3).

Legal/operating/brand names, logo + favicon references, and a flexible list of
contacts (Dave: "the ability to add additional phone numbers and additional
emails ... and additional websites"). Documents + notifications consume it via
:mod:`app.services.company_info` (COMPANY_PHONE-style lookups resolve here
first, env-settings fallback second) — never by reading this table directly.
"""
from sqlalchemy import CheckConstraint, Column, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB

from app.db.base import Base


class PlatformCompanyInfo(Base):
    __tablename__ = "platform_company_info"

    # Single-row table: id is forced to 1 (the accessor upserts row 1).
    id = Column(Integer, primary_key=True, default=1)

    legal_name = Column(String, nullable=False)       # "PaySpyre Financial Inc."
    operating_name = Column(String, nullable=False)   # "PaySpyre Financial"
    brand_name = Column(String, nullable=True)        # "PaySpyre"
    lending_type = Column(String, nullable=True)      # informational (Dave)

    # Storage refs (document-storage keys or absolute URLs) — not blobs.
    logo_ref = Column(String, nullable=True)
    favicon_ref = Column(String, nullable=True)

    # Contacts: ordered list of {"kind": "phone"|"email"|"address"|"website",
    # "label": str, "value": str, "is_primary": bool}. Multiple entries per kind
    # supported by design (per-campaign landing pages etc.).
    contacts = Column(JSONB, nullable=False, default=list)

    updated_by = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_platform_company_info_single_row"),
    )
