import pytest
from datetime import datetime, timedelta
from uuid import uuid4

from app.models.notification import (
    Delivery, Notification, NotificationPreference, NotificationTemplate, WebhookDelivery
)
from app.schemas.notification import (
    BulkNotificationCreate, NotificationCreate, NotificationPreferenceCreate,
    NotificationTemplateCreate, WebhookDeliveryCreate
)
from app.services.notifications import NotificationQueue


@pytest.fixture
def notification_queue(db_session):
    """Create a NotificationQueue instance with test database session."""
    return NotificationQueue(db_session)


class TestNotificationTemplate:
    def test_create_template(self, db_session):
        template_data = NotificationTemplateCreate(
            name="test_template",
            type="email",
            category="application_status",
            subject="Test Subject",
            body_template="<html>Test body: {{ name }}</html>",
            variables=["name"],
        )

        template = NotificationTemplate(**template_data.model_dump())
        db_session.add(template)
        db_session.commit()
        db_session.refresh(template)

        assert template.id is not None
        assert template.name == "test_template"
        assert template.type == "email"
        assert template.is_active is True

    def test_template_unique_name(self, db_session):
        template = NotificationTemplate(
            name="duplicate_test",
            type="email",
            category="system",
            body_template="Test",
        )
        db_session.add(template)
        db_session.commit()

        duplicate = NotificationTemplate(
            name="duplicate_test",
            type="sms",
            category="system",
            body_template="Test 2",
        )
        db_session.add(duplicate)

        with pytest.raises(Exception):
            db_session.commit()


class TestNotificationQueue:
    @pytest.mark.asyncio
    async def test_queue_notification(self, notification_queue):
        notification_data = NotificationCreate(
            type="email",
            recipient="test@example.com",
            subject="Test Notification",
            body="<html>Test</html>",
            priority="normal",
        )

        notification = await notification_queue.queue_notification(notification_data.model_dump())

        assert notification.id is not None
        assert notification.status == "queued"
        assert notification.recipient == "test@example.com"

    @pytest.mark.asyncio
    async def test_queue_bulk_notifications(self, notification_queue):
        recipients = ["user1@example.com", "user2@example.com", "user3@example.com"]

        notifications = await notification_queue.queue_bulk_notifications(
            recipients=recipients,
            notification_data={
                "type": "email",
                "subject": "Bulk Test",
                "body": "Test body",
            },
        )

        assert len(notifications) == 3
        assert all(n.status == "queued" for n in notifications)

    def test_get_queue_stats(self, notification_queue):
        for _ in range(5):
            notification = Notification(
                type="email",
                recipient="test@example.com",
                body="Test",
                status="queued",
            )
            notification_queue.db.add(notification)

        notification_queue.db.commit()

        stats = notification_queue.get_queue_stats()

        assert stats["queued"] >= 5


class TestNotificationPreferences:
    def test_create_preferences(self, db_session):
        prefs_data = NotificationPreferenceCreate(
            user_id=uuid4(),
            email_enabled=True,
            sms_enabled=False,
            quiet_hours_start="22:00",
            quiet_hours_end="08:00",
        )

        preferences = NotificationPreference(**prefs_data.model_dump())
        db_session.add(preferences)
        db_session.commit()
        db_session.refresh(preferences)

        assert preferences.id is not None
        assert preferences.email_enabled is True
        assert preferences.sms_enabled is False
        assert preferences.quiet_hours_start == "22:00"

    def test_time_format_validation(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            NotificationPreferenceCreate(
                user_id=uuid4(),
                quiet_hours_start="invalid",
            )

        with pytest.raises(ValidationError):
            NotificationPreferenceCreate(
                user_id=uuid4(),
                quiet_hours_start="25:00",
            )


class TestWebhookDelivery:
    def test_create_webhook_delivery(self, db_session):
        webhook_data = WebhookDeliveryCreate(
            vendor_id=uuid4(),
            event_type="application.approved",
            url="https://example.com/webhook",
            payload={"application_id": str(uuid4()), "status": "approved"},
        )

        delivery = WebhookDelivery(**webhook_data.model_dump())
        db_session.add(delivery)
        db_session.commit()
        db_session.refresh(delivery)

        assert delivery.id is not None
        assert delivery.status == "pending"
        assert delivery.attempt_number == 1
        assert delivery.payload["status"] == "approved"


class TestDelivery:
    def test_create_delivery(self, db_session):
        notification = Notification(
            type="email",
            recipient="test@example.com",
            body="Test",
            status="sent",
        )
        db_session.add(notification)
        db_session.commit()

        delivery = Delivery(
            notification_id=notification.id,
            attempt_number=1,
            status="sent",
            provider="resend",
            provider_message_id="msg_123",
            response={"message": "sent"},
        )
        db_session.add(delivery)
        db_session.commit()
        db_session.refresh(delivery)

        assert delivery.id is not None
        assert delivery.status == "sent"
        assert delivery.provider_message_id == "msg_123"


class TestTemplateRenderer:
    @pytest.mark.asyncio
    async def test_render_from_string(self, notification_queue):
        template = "Hello {{ name }}, your balance is {{ balance }}."
        variables = {"name": "John", "balance": "$100"}

        result = notification_queue.renderer.render_from_string(template, variables)

        assert result == "Hello John, your balance is $100."


class TestRetryLogic:
    def test_should_retry_by_priority(self, notification_queue):
        notification_high = Notification(
            type="email",
            recipient="test@example.com",
            body="Test",
            status="failed",
            priority="high",
        )
        notification_queue.db.add(notification_high)
        notification_queue.db.commit()

        assert notification_queue._should_retry(notification_high) is True

    def test_should_not_retry_low_priority(self, notification_queue):
        for _ in range(3):
            delivery = Delivery(
                notification_id=uuid4(),
                attempt_number=1,
                status="failed",
                provider="resend",
            )
            notification_queue.db.add(delivery)

        notification_low = Notification(
            type="email",
            recipient="test@example.com",
            body="Test",
            status="failed",
            priority="low",
        )
        notification_queue.db.add(notification_low)
        notification_queue.db.commit()

        assert notification_queue._should_retry(notification_low) is False