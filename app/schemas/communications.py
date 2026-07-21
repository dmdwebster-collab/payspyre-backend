"""Request/response shapes for the admin communications hub (WS-A).

The cockpit surface over the append-only ``platform_communications_log``:
list the legal comms record (full bodies), compose a templated email/SMS to a
borrower, and log an offline contact (phone call / in-person — mandatory
comment, the TL "Log message" dialog).
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

Channel = Literal["email", "sms", "dashboard", "offline"]
SendChannel = Literal["email", "sms"]
OfflineMethod = Literal["phone", "in_person", "video", "mail", "other"]
Direction = Literal["outbound", "inbound"]


class CommunicationOut(BaseModel):
    id: UUID
    channel: Channel
    direction: Direction
    recipient: Optional[str] = None
    subject: Optional[str] = None
    body: str
    notification_type: Optional[str] = None
    patient_id: Optional[UUID] = None
    application_id: Optional[UUID] = None
    loan_id: Optional[UUID] = None
    sent_by: str
    sent_by_user_id: Optional[UUID] = None
    comment: Optional[str] = None
    contact_method: Optional[str] = None
    purpose: Optional[str] = None
    result: Optional[str] = None
    status: str
    vendor: Optional[str] = None
    vendor_message_id: Optional[str] = None
    source_event_id: Optional[int] = None
    occurred_at: datetime
    created_at: datetime

    model_config = {"from_attributes": True}


class TemplatedSendBody(BaseModel):
    """Compose a templated email/SMS to a borrower from the cockpit."""

    patient_id: UUID
    notification_type: str = Field(..., min_length=1, max_length=200)
    channel: SendChannel = "email"
    # Merge context for the template (borrower_name, payment_amount, ...).
    context: dict = Field(default_factory=dict)
    application_id: Optional[UUID] = None
    loan_id: Optional[UUID] = None
    # Internal note ("why this was sent") — stored on the log row.
    comment: Optional[str] = Field(None, max_length=2000)


class TemplatedSendOut(BaseModel):
    status: str
    source_event_id: Optional[int] = None
    communication: CommunicationOut


class TemplatePreviewBody(BaseModel):
    notification_type: str = Field(..., min_length=1, max_length=200)
    channel: SendChannel = "email"
    context: dict = Field(default_factory=dict)


class TemplatePreviewOut(BaseModel):
    notification_type: str
    channel: SendChannel
    subject: Optional[str] = None
    body: str


class TemplateInfo(BaseModel):
    """One registry entry — populates the cockpit's Use Template dropdown."""

    notification_type: str
    channels: list[SendChannel]
    email_subject_template: str


class OfflineContactBody(BaseModel):
    """TL "Log message" dialog: Date* / Method* / Purpose* / Result / Comment*.

    The comment is MANDATORY (spec): a contact log without the narrative is
    not evidence. At least one of patient/application/loan anchors it.
    """

    patient_id: Optional[UUID] = None
    application_id: Optional[UUID] = None
    loan_id: Optional[UUID] = None
    contact_method: OfflineMethod
    direction: Direction = "outbound"
    occurred_at: Optional[datetime] = None
    purpose: str = Field(..., min_length=1, max_length=500)
    result: Optional[str] = Field(None, max_length=500)
    comment: str = Field(..., min_length=1, max_length=5000)
