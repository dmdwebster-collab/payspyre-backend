from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class NotificationTemplateCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    type: str = Field(..., pattern="^(email|sms)$")
    category: str = Field(..., pattern="^(application_status|payment_reminder|statement|urgent|marketing|system)$")
    subject: str | None = None
    body_template: str = Field(..., min_length=1)
    variables: list[str] = []


class NotificationTemplateUpdate(BaseModel):
    subject: str | None = None
    body_template: str | None = None
    variables: list[str] | None = None
    is_active: bool | None = None


class NotificationTemplateResponse(BaseModel):
    id: UUID
    name: str
    type: str
    category: str
    subject: str | None
    body_template: str
    variables: list[str]
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class NotificationCreate(BaseModel):
    user_id: UUID | None = None
    loan_application_id: UUID | None = None
    vendor_id: UUID | None = None
    webhook_url: str | None = None
    type: str = Field(..., pattern="^(email|sms|webhook)$")
    template_id: UUID | None = None
    priority: str = Field(default="normal", pattern="^(low|normal|high|urgent)$")
    recipient: str = Field(..., min_length=1)
    subject: str | None = None
    body: str = Field(..., min_length=1)
    variables: dict = {}
    scheduled_for: datetime | None = None


class NotificationUpdate(BaseModel):
    status: str | None = Field(None, pattern="^(queued|processing|sent|delivered|failed|retrying)$")
    scheduled_for: datetime | None = None


class NotificationResponse(BaseModel):
    id: UUID
    user_id: UUID | None
    loan_application_id: UUID | None
    vendor_id: UUID | None
    webhook_url: str | None
    type: str
    template_id: UUID | None
    priority: str
    status: str
    recipient: str
    subject: str | None
    body: str
    variables: dict
    scheduled_for: datetime | None
    sent_at: datetime | None
    delivered_at: datetime | None
    failed_at: datetime | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class NotificationPreferenceCreate(BaseModel):
    user_id: UUID
    vendor_id: UUID | None = None
    email_enabled: bool = True
    sms_enabled: bool = False
    application_status_enabled: bool = True
    payment_reminders_enabled: bool = True
    statements_enabled: bool = True
    urgent_notifications_enabled: bool = True
    marketing_enabled: bool = False
    daily_digest_enabled: bool = False
    quiet_hours_start: str | None = None
    quiet_hours_end: str | None = None

    @field_validator("quiet_hours_start", "quiet_hours_end")
    @classmethod
    def validate_time_format(cls, v):
        if v is None:
            return v
        if not len(v) == 5 or v[2] != ":":
            raise ValueError("Time must be in HH:MM format")
        try:
            hour = int(v[:2])
            minute = int(v[3:])
            if not (0 <= hour <= 23) or not (0 <= minute <= 59):
                raise ValueError
        except ValueError:
            raise ValueError("Invalid time values")
        return v


class NotificationPreferenceUpdate(BaseModel):
    email_enabled: bool | None = None
    sms_enabled: bool | None = None
    application_status_enabled: bool | None = None
    payment_reminders_enabled: bool | None = None
    statements_enabled: bool | None = None
    urgent_notifications_enabled: bool | None = None
    marketing_enabled: bool | None = None
    daily_digest_enabled: bool | None = None
    quiet_hours_start: str | None = None
    quiet_hours_end: str | None = None


class NotificationPreferenceResponse(BaseModel):
    id: UUID
    user_id: UUID
    vendor_id: UUID | None
    email_enabled: bool
    sms_enabled: bool
    application_status_enabled: bool
    payment_reminders_enabled: bool
    statements_enabled: bool
    urgent_notifications_enabled: bool
    marketing_enabled: bool
    daily_digest_enabled: bool
    quiet_hours_start: str | None
    quiet_hours_end: str | None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class DeliveryResponse(BaseModel):
    id: UUID
    notification_id: UUID
    attempt_number: int
    status: str
    provider: str
    provider_message_id: str | None
    response: dict | None
    error: str | None
    delivered_at: datetime | None
    created_at: datetime

    class Config:
        from_attributes = True


class WebhookDeliveryCreate(BaseModel):
    vendor_id: UUID
    event_type: str = Field(..., min_length=1)
    url: str = Field(..., min_length=1)
    payload: dict


class WebhookDeliveryResponse(BaseModel):
    id: UUID
    vendor_id: UUID
    event_type: str
    url: str
    payload: dict
    status: str
    response_code: int | None
    response_body: str | None
    attempt_number: int
    retry_after: datetime | None
    created_at: datetime
    delivered_at: datetime | None

    class Config:
        from_attributes = True


class NotificationQueueResponse(BaseModel):
    queued_count: int
    processing_count: int
    failed_count: int
    retry_count: int


class BulkNotificationCreate(BaseModel):
    recipients: list[str] = Field(..., min_length=1)
    type: str = Field(..., pattern="^(email|sms)$")
    template_id: UUID | None = None
    subject: str | None = None
    body: str = Field(..., min_length=1)
    variables: list[dict] = []
    scheduled_for: datetime | None = None
    priority: str = Field(default="normal", pattern="^(low|normal|high|urgent)$")


class BulkNotificationResponse(BaseModel):
    notification_ids: list[UUID]
    total: int
    success: int
    failed: int