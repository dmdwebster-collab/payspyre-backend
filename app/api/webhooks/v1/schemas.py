"""Pydantic models for vendor webhook payloads (P6.6).

For the MVP all three vendors share a common envelope mirroring
``mock_dispatcher.simulate_callback`` output, plus ``application_id`` /
``verification_id`` so the receiver can call the orchestrator. The body is parsed
MANUALLY (after signature verification), not via FastAPI auto-parse.
"""
from __future__ import annotations

from typing import Any, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class WebhookVerificationBody(BaseModel):
    verification_type: str
    result: Literal["passed", "failed"]
    application_id: UUID
    verification_id: UUID
    rich_payload: dict[str, Any] = Field(default_factory=dict)
    vendor: Optional[str] = None  # informational; the path param is authoritative


class WebhookAcceptedResponse(BaseModel):
    status: str
    application_status: Optional[str] = None
    decided: bool = False
