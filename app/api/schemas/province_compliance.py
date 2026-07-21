"""Pydantic schemas for the per-province compliance rule admin API."""
from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


class ProvinceComplianceRuleBase(BaseModel):
    province_name: Optional[str] = Field(default=None, max_length=64)
    enabled: Optional[bool] = None
    apr_cap_bps: Optional[int] = Field(default=None, gt=0)
    high_cost_apr_threshold_bps: Optional[int] = Field(default=None, gt=0)
    high_cost_license_held: Optional[bool] = None
    license_required: Optional[bool] = None
    license_notes: Optional[str] = None
    comms_window_start_hour: Optional[int] = Field(default=None, ge=0, le=23)
    comms_window_end_hour: Optional[int] = Field(default=None, ge=0, le=23)
    comms_max_contacts_per_week: Optional[int] = Field(default=None, ge=0)
    required_disclosures: Optional[list[str]] = None
    language_requirement: Optional[str] = Field(default=None, max_length=16)
    quebec_language_required: Optional[bool] = None
    counsel_confirmed: Optional[bool] = None
    notes: Optional[str] = None


class ProvinceComplianceRuleUpsert(ProvinceComplianceRuleBase):
    """Body for creating/replacing a province rule (province_code in the path)."""


class ProvinceComplianceRuleUpdate(ProvinceComplianceRuleBase):
    """Body for patching a province rule (all fields optional)."""


class ProvinceComplianceRuleRead(BaseModel):
    id: UUID
    province_code: str
    province_name: str
    enabled: bool
    apr_cap_bps: Optional[int] = None
    high_cost_apr_threshold_bps: Optional[int] = None
    high_cost_license_held: bool
    license_required: bool
    license_notes: Optional[str] = None
    comms_window_start_hour: Optional[int] = None
    comms_window_end_hour: Optional[int] = None
    comms_max_contacts_per_week: Optional[int] = None
    required_disclosures: list[str] = Field(default_factory=list)
    language_requirement: Optional[str] = None
    quebec_language_required: bool
    counsel_confirmed: bool
    notes: Optional[str] = None
    updated_by: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    version: int

    class Config:
        from_attributes = True
