from datetime import datetime
from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class CreditProductBase(BaseModel):
    code: str = Field(..., min_length=1, max_length=100)
    name: str = Field(..., min_length=1, max_length=200)
    vertical: Literal["dental", "auto", "veterinary"]
    min_amount_cents: int = Field(..., gt=0)
    max_amount_cents: int = Field(..., gt=0)
    currency: str = Field(default="CAD", min_length=3, max_length=3)
    verification_matrix: dict[str, Any]
    decision_ruleset: str = Field(..., min_length=1, max_length=200)
    # Wire type stays a dict for back-compat (legacy shapes are accepted), but
    # create/PATCH validate it against app.schemas.pricing_config.PricingConfig
    # (+ per-frequency s.347 APR gates) and persist the normalized typed shape.
    pricing_config: dict[str, Any]
    funding_source: Literal["payspyre_capital", "partner_lender", "hybrid", "clinic_self"]
    created_by: Optional[UUID] = None

    class Config:
        from_attributes = True


class CreditProductCreate(CreditProductBase):
    status: Literal["draft", "active", "archived"] = "draft"

    class Config:
        from_attributes = True


class CreditProductUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    status: Optional[Literal["draft", "active", "archived"]] = None
    min_amount_cents: Optional[int] = Field(default=None, gt=0)
    max_amount_cents: Optional[int] = Field(default=None, gt=0)
    currency: Optional[str] = Field(default=None, min_length=3, max_length=3)
    verification_matrix: Optional[dict[str, Any]] = None
    decision_ruleset: Optional[str] = Field(default=None, min_length=1, max_length=200)
    pricing_config: Optional[dict[str, Any]] = None
    funding_source: Optional[Literal["payspyre_capital", "partner_lender", "hybrid", "clinic_self"]] = None

    class Config:
        from_attributes = True


class CreditProductRead(CreditProductBase):
    id: UUID
    status: Literal["draft", "active", "archived"]
    created_at: datetime
    updated_at: datetime
    version: int

    class Config:
        from_attributes = True
