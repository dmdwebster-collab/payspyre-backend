"""Per-province compliance rule table (Workstream W2 — Turnkey parity, videos 07-08).

Net-new beyond Turnkey Lender (TL only has a static province checkbox list). One
row per Canadian province/territory. The row encodes the regulatory guardrails
Dave wants enforced at product-configuration time:

* ``apr_cap_bps`` — hard maximum disclosed APR; a config that can reach it is
  BLOCKED at product create/update (defense-in-depth on top of the federal
  Criminal Code s.347 cap enforced in ``app.services.loan_quote``).
* ``high_cost_apr_threshold_bps`` + ``high_cost_license_held`` — the APR above
  which provincial high-cost-credit *licensing* kicks in; blocks only when the
  licence is not on file.
* ``license_required`` — provincial alternative-lender licence required
  regardless of APR (e.g. Saskatchewan). Surfaced as a warning flag.
* ``comms_window_start_hour`` / ``comms_window_end_hour`` /
  ``comms_max_contacts_per_week`` — provincial Consumer-Protection-Act contact
  windows and frequency caps (consumed by the comms engine).
* ``required_disclosures`` — list of disclosure keys the province mandates.
* ``language_requirement`` / ``quebec_language_required`` — Quebec-language
  (Charter of the French Language) requirement seed.

EVERY seeded numeric value is a conservative PLACEHOLDER. ``counsel_confirmed``
is ``False`` until Dave/legal confirm the province-specific inputs; the admin
API surfaces every unconfirmed rule so nobody mistakes a placeholder for an
authoritative legal number.
"""
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.db.base import Base


class PlatformProvinceComplianceRule(Base):
    __tablename__ = "platform_province_compliance_rules"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)

    # ISO 3166-2:CA subdivision code without the "CA-" prefix (e.g. "ON", "QC").
    province_code = Column(String(2), nullable=False, unique=True)
    province_name = Column(String(64), nullable=False)

    # Whether PaySpyre operates in this province today. QC ships disabled,
    # preserving the existing Quebec gate until French-language docs land.
    enabled = Column(Boolean, nullable=False, default=True)

    # --- APR / high-cost-credit -------------------------------------------
    apr_cap_bps = Column(Integer, nullable=True)
    high_cost_apr_threshold_bps = Column(Integer, nullable=True)
    high_cost_license_held = Column(Boolean, nullable=False, default=False)

    # --- Licensing ---------------------------------------------------------
    license_required = Column(Boolean, nullable=False, default=False)
    license_notes = Column(Text, nullable=True)

    # --- Communications (provincial CPA) ----------------------------------
    comms_window_start_hour = Column(Integer, nullable=True)
    comms_window_end_hour = Column(Integer, nullable=True)
    comms_max_contacts_per_week = Column(Integer, nullable=True)

    # --- Disclosures / language -------------------------------------------
    required_disclosures = Column(JSONB, nullable=False, default=list)
    language_requirement = Column(String(16), nullable=True)
    quebec_language_required = Column(Boolean, nullable=False, default=False)

    # --- Governance --------------------------------------------------------
    counsel_confirmed = Column(Boolean, nullable=False, default=False)
    notes = Column(Text, nullable=True)
    updated_by = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    version = Column(Integer, nullable=False, default=1)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<PlatformProvinceComplianceRule(code={self.province_code}, "
            f"enabled={self.enabled}, counsel_confirmed={self.counsel_confirmed})>"
        )
