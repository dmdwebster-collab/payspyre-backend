"""Request/response shapes for application message threads.

Shared by the clinic (vendor-facing) API and the admin (whole-book) API so both
sides speak the same contract. ``sender_kind`` tells the frontend which side
authored a message ('vendor' = the clinic, 'admin' = PaySpyre); ``mine`` tells
it whether the *current viewer* authored it (for left/right bubble rendering).
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

SenderKind = Literal["vendor", "admin"]


class MessageOut(BaseModel):
    id: UUID
    application_id: UUID
    sender_kind: SenderKind
    sender_name: str
    body: str
    created_at: datetime
    mine: bool = False


class MessageCreate(BaseModel):
    body: str = Field(..., min_length=1, max_length=10_000)


class MarkReadOut(BaseModel):
    application_id: UUID
    unread_count: int = 0


class UnreadCountOut(BaseModel):
    unread_count: int


class ThreadSummary(BaseModel):
    """One row in the "channel list" — an application with message activity."""

    application_id: UUID
    patient_name: str
    message_count: int
    unread_count: int
    last_message_at: Optional[datetime] = None
    last_message_preview: Optional[str] = None
