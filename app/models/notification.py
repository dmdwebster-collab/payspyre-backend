from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    Boolean, Column, DateTime, Enum, ForeignKey, Index, Integer, String, Text,
    func, text
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from app.db.base import Base


class NotificationTemplate(Base):
    __tablename__ = "notification_templates"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    name = Column(String(100), nullable=False, unique=True)
    type = Column(
        Enum("email", "sms", name="notification_type"),
        nullable=False,
    )
    category = Column(
        Enum(
            "application_status", "payment_reminder", "statement",
            "urgent", "marketing", "system", name="template_category"
        ),
        nullable=False,
    )
    subject = Column(String(255), nullable=True)
    body_template = Column(Text, nullable=False)
    variables = Column(JSONB, nullable=True, default=list)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_template_type", "type"),
        Index("idx_template_category", "category"),
    )


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    loan_application_id = Column(UUID(as_uuid=True), ForeignKey("loan_applications.id"), nullable=True)
    vendor_id = Column(UUID(as_uuid=True), ForeignKey("vendors.id"), nullable=True)
    webhook_url = Column(Text, nullable=True)
    type = Column(
        Enum("email", "sms", "webhook", name="notification_type"),
        nullable=False,
    )
    template_id = Column(UUID(as_uuid=True), ForeignKey("notification_templates.id"), nullable=True)
    priority = Column(
        Enum("low", "normal", "high", "urgent", name="notification_priority"),
        nullable=False,
        default="normal",
    )
    status = Column(
        Enum(
            "queued", "processing", "sent", "delivered", "failed",
            "retrying", name="notification_status"
        ),
        nullable=False,
        default="queued",
    )
    recipient = Column(String(255), nullable=False)
    subject = Column(String(255), nullable=True)
    body = Column(Text, nullable=False)
    variables = Column(JSONB, nullable=True, default=dict)
    scheduled_for = Column(DateTime(timezone=True), nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    failed_at = Column(DateTime(timezone=True), nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    deliveries = relationship("Delivery", back_populates="notification", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_notification_user", "user_id"),
        Index("idx_notification_status", "status"),
        Index("idx_notification_priority", "priority"),
        Index("idx_notification_type", "type"),
        Index("idx_notification_scheduled", "scheduled_for"),
        Index("idx_notification_loan_app", "loan_application_id"),
        Index("idx_notification_vendor", "vendor_id"),
    )


class Delivery(Base):
    __tablename__ = "deliveries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    notification_id = Column(UUID(as_uuid=True), ForeignKey("notifications.id"), nullable=False)
    attempt_number = Column(Integer, nullable=False, default=1)
    status = Column(
        Enum("pending", "sent", "delivered", "failed", name="delivery_status"),
        nullable=False,
        default="pending",
    )
    provider = Column(String(50), nullable=False)
    provider_message_id = Column(String(255), nullable=True)
    response = Column(JSONB, nullable=True)
    error = Column(Text, nullable=True)
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    notification = relationship("Notification", back_populates="deliveries")

    __table_args__ = (
        Index("idx_delivery_notification", "notification_id"),
        Index("idx_delivery_status", "status"),
    )


class NotificationPreference(Base):
    __tablename__ = "notification_preferences"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    vendor_id = Column(UUID(as_uuid=True), ForeignKey("vendors.id"), nullable=True)
    email_enabled = Column(Boolean, nullable=False, default=True)
    sms_enabled = Column(Boolean, nullable=False, default=False)
    application_status_enabled = Column(Boolean, nullable=False, default=True)
    payment_reminders_enabled = Column(Boolean, nullable=False, default=True)
    statements_enabled = Column(Boolean, nullable=False, default=True)
    urgent_notifications_enabled = Column(Boolean, nullable=False, default=True)
    marketing_enabled = Column(Boolean, nullable=False, default=False)
    daily_digest_enabled = Column(Boolean, nullable=False, default=False)
    quiet_hours_start = Column(String(5), nullable=True)  # Format: HH:MM
    quiet_hours_end = Column(String(5), nullable=True)  # Format: HH:MM
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("idx_notification_prefs_user", "user_id"),
        Index("idx_notification_prefs_vendor", "vendor_id"),
    )


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    vendor_id = Column(UUID(as_uuid=True), ForeignKey("vendors.id"), nullable=False)
    event_type = Column(String(100), nullable=False)
    url = Column(Text, nullable=False)
    payload = Column(JSONB, nullable=False)
    status = Column(
        Enum("pending", "sent", "failed", name="webhook_status"),
        nullable=False,
        default="pending",
    )
    response_code = Column(Integer, nullable=True)
    response_body = Column(Text, nullable=True)
    attempt_number = Column(Integer, nullable=False, default=1)
    retry_after = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    delivered_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("idx_webhook_vendor", "vendor_id"),
        Index("idx_webhook_event", "event_type"),
        Index("idx_webhook_status", "status"),
    )