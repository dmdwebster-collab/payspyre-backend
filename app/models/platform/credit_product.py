from datetime import datetime
from uuid import UUID, uuid4
from typing import Optional, Any

from sqlalchemy import Column, DateTime, String, Integer, BigInteger, func, Text
from sqlalchemy.dialects.postgresql import UUID, JSONB, ENUM
from sqlalchemy.orm import relationship

from app.db.base import Base


class PlatformCreditProduct(Base):
    __tablename__ = "platform_credit_products"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    code = Column(String, nullable=False, unique=True)
    name = Column(String, nullable=False)
    vertical = Column(
        ENUM("dental", "auto", "veterinary", name="platform_vertical", create_type=False),
        nullable=False
    )
    status = Column(
        ENUM("draft", "active", "archived", name="platform_credit_product_status", create_type=False),
        nullable=False,
        default="draft"
    )

    min_amount_cents = Column(BigInteger, nullable=False)
    max_amount_cents = Column(BigInteger, nullable=False)
    currency = Column(String, nullable=False, default="CAD")

    # The verification matrix: per amount bracket, which verifications run
    verification_matrix = Column(JSONB, nullable=False)

    # Decision rules — references to YAML rule files
    decision_ruleset = Column(String, nullable=False)

    # Pricing configuration
    pricing_config = Column(JSONB, nullable=False)

    # Product-policy configuration (grace period / due dates / due-date seasons /
    # payoff / disbursement / approval / repayment modes). Nullable — NULL means
    # the ProductPolicyConfig defaults apply (current engine behaviour). See
    # app/schemas/product_policy_config.py.
    policy_config = Column(JSONB, nullable=True)

    # Funding model
    funding_source = Column(
        ENUM("payspyre_capital", "partner_lender", "hybrid", "clinic_self",
             name="platform_funding_source", create_type=False),
        nullable=False
    )

    created_by = Column(UUID(as_uuid=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    version = Column(Integer, nullable=False, default=1)

    # Relationships
    applications = relationship("PlatformCreditApplication", back_populates="credit_product")

    def __repr__(self) -> str:
        return f"<PlatformCreditProduct(code={self.code}, name={self.name}, status={self.status})>"
