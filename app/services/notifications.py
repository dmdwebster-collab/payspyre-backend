import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import resend
from jinja2 import Template
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.notification import (
    Delivery, Notification, NotificationPreference, NotificationTemplate, WebhookDelivery
)

logger = logging.getLogger(__name__)


class EmailSender:
    def __init__(self):
        resend.api_key = settings.RESEND_API_KEY

    async def send(
        self,
        to: str,
        subject: str,
        html_content: str,
        from_email: str | None = None,
        reply_to: str | None = None,
    ) -> tuple[bool, str, dict | None]:
        try:
            from_email = from_email or settings.RESEND_FROM_EMAIL

            params: dict[str, Any] = {
                "from": from_email,
                "to": to,
                "subject": subject,
                "html": html_content,
            }

            if reply_to:
                params["reply_to"] = reply_to

            result = resend.Emails.send(params)
            return True, result.get("id", ""), result

        except Exception as e:
            logger.error(f"Email send failed: {e}")
            return False, str(e), None


class SmsSender:
    def __init__(self):
        from twilio.rest import Client
        self.client = Client(
            settings.TWILIO_ACCOUNT_SID,
            settings.TWILIO_AUTH_TOKEN,
        )

    async def send(
        self,
        to: str,
        message: str,
        from_number: str | None = None,
    ) -> tuple[bool, str, dict | None]:
        try:
            from_number = from_number or settings.TWILIO_FROM_NUMBER

            message_obj = self.client.messages.create(
                body=message,
                from_=from_number,
                to=to,
            )

            return True, message_obj.sid, {
                "status": message_obj.status,
                "direction": message_obj.direction,
            }

        except Exception as e:
            logger.error(f"SMS send failed: {e}")
            return False, str(e), None


class WebhookSender:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=30.0)

    async def send(
        self,
        url: str,
        payload: dict,
        headers: dict | None = None,
    ) -> tuple[bool, int, str | None, dict | None]:
        try:
            default_headers = {
                "Content-Type": "application/json",
                "User-Agent": "PaySpyre-Webhook/1.0",
            }

            if headers:
                default_headers.update(headers)

            response = await self.client.post(url, json=payload, headers=default_headers)

            response_data = None
            if response.headers.get("content-type", "").startswith("application/json"):
                try:
                    response_data = response.json()
                except Exception:
                    response_data = {"raw": response.text}

            return (
                200 <= response.status_code < 300,
                response.status_code,
                response.text if response.text else None,
                response_data,
            )

        except httpx.TimeoutException:
            logger.error(f"Webhook timeout: {url}")
            return False, 504, "Request timeout", None

        except httpx.HTTPError as e:
            logger.error(f"Webhook HTTP error: {e}")
            return False, 500, str(e), None

        except Exception as e:
            logger.error(f"Webhook send failed: {e}")
            return False, 500, str(e), None

    async def close(self):
        await self.client.aclose()


class TemplateRenderer:
    def __init__(self):
        self.template_dir = os.path.join(os.path.dirname(__file__), "..", "templates", "emails")

    def render(self, template_name: str, variables: dict) -> tuple[str, str]:
        template_path = os.path.join(self.template_dir, f"{template_name}.html")

        if not os.path.exists(template_path):
            raise FileNotFoundError(f"Template not found: {template_path}")

        with open(template_path, "r", encoding="utf-8") as f:
            template_content = f.read()

        template = Template(template_content)
        rendered_html = template.render(**variables)

        subject = variables.get("subject", "Notification from PaySpyre")

        return subject, rendered_html

    def render_from_string(self, template_string: str, variables: dict) -> str:
        template = Template(template_string)
        return template.render(**variables)


class NotificationQueue:
    def __init__(self, db: Session):
        self.db = db
        self.email_sender = EmailSender()
        self.sms_sender = SmsSender()
        self.webhook_sender = WebhookSender()
        self.renderer = TemplateRenderer()
        self._running = False

    async def queue_notification(
        self,
        notification_data: dict,
    ) -> Notification:
        notification = Notification(**notification_data)
        self.db.add(notification)
        self.db.commit()
        self.db.refresh(notification)
        return notification

    async def queue_bulk_notifications(
        self,
        recipients: list[str],
        notification_data: dict,
    ) -> list[Notification]:
        notifications = []
        for recipient in recipients:
            data = notification_data.copy()
            data["recipient"] = recipient

            if "variables" in data and isinstance(data["variables"], list):
                idx = len(notifications)
                if idx < len(data["variables"]):
                    data["variables"] = data["variables"][idx]

            notification = Notification(**data)
            self.db.add(notification)
            notifications.append(notification)

        self.db.commit()
        for n in notifications:
            self.db.refresh(n)

        return notifications

    async def process_queue(self) -> dict[str, int]:
        now = datetime.utcnow()

        queued = self.db.query(Notification).filter(
            Notification.status == "queued",
            Notification.scheduled_for <= now,
        ).order_by(Notification.priority.desc(), Notification.created_at).all()

        count = {"processed": 0, "success": 0, "failed": 0}

        for notification in queued:
            try:
                notification.status = "processing"
                self.db.commit()

                success = await self._send_notification(notification)

                if success:
                    notification.status = "sent"
                    notification.sent_at = now
                    count["success"] += 1
                else:
                    notification.status = "failed"
                    notification.failed_at = now
                    count["failed"] += 1

                count["processed"] += 1

            except Exception as e:
                logger.error(f"Error processing notification {notification.id}: {e}")
                notification.status = "failed"
                notification.failed_at = now
                notification.error_message = str(e)
                count["failed"] += 1
                count["processed"] += 1

            finally:
                self.db.commit()

        return count

    async def _send_notification(self, notification: Notification) -> bool:
        success = False
        message_id = ""
        response = None
        error = None

        delivery = Delivery(
            notification_id=notification.id,
            attempt_number=self._get_next_attempt(notification.id),
            provider="resend" if notification.type == "email" else "twilio",
        )

        try:
            if notification.type == "email":
                success, message_id, response = await self.email_sender.send(
                    to=notification.recipient,
                    subject=notification.subject or "Notification",
                    html_content=notification.body,
                )

            elif notification.type == "sms":
                success, message_id, response = await self.sms_sender.send(
                    to=notification.recipient,
                    message=notification.body,
                )

            elif notification.type == "webhook" and notification.webhook_url:
                success, status_code, error, response = await self.webhook_sender.send(
                    url=notification.webhook_url,
                    payload=notification.variables,
                )

            if success:
                delivery.status = "sent"
                delivery.provider_message_id = message_id
                delivery.response = response
                delivery.delivered_at = datetime.utcnow()

                notification.delivered_at = datetime.utcnow()
                notification.status = "delivered"
            else:
                delivery.status = "failed"
                delivery.error = error or "Unknown error"

                if self._should_retry(notification):
                    await self._schedule_retry(notification)

        except Exception as e:
            delivery.status = "failed"
            delivery.error = str(e)
            logger.error(f"Send failed for notification {notification.id}: {e}")

        finally:
            self.db.add(delivery)

        return success

    def _get_next_attempt(self, notification_id: UUID) -> int:
        attempt = self.db.query(Delivery).filter(
            Delivery.notification_id == notification_id
        ).count()
        return max(1, attempt + 1)

    def _should_retry(self, notification: Notification) -> bool:
        if notification.priority in ["high", "urgent"]:
            return True

        deliveries = self.db.query(Delivery).filter(
            Delivery.notification_id == notification.id
        ).count()

        return deliveries < 3

    async def _schedule_retry(self, notification: Notification) -> None:
        deliveries = self.db.query(Delivery).filter(
            Delivery.notification_id == notification.id
        ).count()

        delays = [5, 15, 30]
        delay_minutes = delays[min(deliveries, len(delays) - 1)]

        notification.status = "queued"
        notification.scheduled_for = datetime.utcnow() + timedelta(minutes=delay_minutes)

    async def retry_failed_notifications(self) -> dict[str, int]:
        now = datetime.utcnow()
        retry_window = now - timedelta(hours=24)

        failed = self.db.query(Notification).filter(
            Notification.status == "failed",
            Notification.failed_at >= retry_window,
        ).all()

        count = {"retried": 0, "success": 0, "failed": 0}

        for notification in failed:
            if not self._should_retry(notification):
                continue

            try:
                notification.status = "queued"
                notification.scheduled_for = now
                notification.failed_at = None
                notification.error_message = None
                count["retried"] += 1

            except Exception as e:
                logger.error(f"Error scheduling retry for {notification.id}: {e}")
                count["failed"] += 1

        self.db.commit()

        for notification in failed:
            if notification.status == "queued":
                success = await self._send_notification(notification)
                if success:
                    count["success"] += 1
                else:
                    count["failed"] += 1

        return count

    async def send_webhook(
        self,
        vendor_id: UUID,
        event_type: str,
        url: str,
        payload: dict,
    ) -> tuple[bool, str]:
        delivery = WebhookDelivery(
            vendor_id=vendor_id,
            event_type=event_type,
            url=url,
            payload=payload,
        )
        self.db.add(delivery)
        self.db.commit()
        self.db.refresh(delivery)

        success, status_code, response_body, response_data = await self.webhook_sender.send(
            url=url,
            payload=payload,
        )

        if success:
            delivery.status = "sent"
            delivery.response_code = status_code
            delivery.response_body = response_body
            delivery.delivered_at = datetime.utcnow()
        else:
            delivery.status = "failed"
            delivery.response_code = status_code
            delivery.response_body = response_body

            if self._should_retry_webhook(delivery):
                await self._schedule_webhook_retry(delivery)

        self.db.commit()
        self.db.refresh(delivery)

        return success, delivery.id

    def _should_retry_webhook(self, delivery: WebhookDelivery) -> bool:
        return delivery.attempt_number < 3

    async def _schedule_webhook_retry(self, delivery: WebhookDelivery) -> None:
        delays = [1, 5, 15]
        delay_minutes = delays[min(delivery.attempt_number - 1, len(delays) - 1)]

        delivery.status = "pending"
        delivery.attempt_number += 1
        delivery.retry_after = datetime.utcnow() + timedelta(minutes=delay_minutes)

    async def retry_pending_webhooks(self) -> dict[str, int]:
        now = datetime.utcnow()

        pending = self.db.query(WebhookDelivery).filter(
            WebhookDelivery.status == "pending",
            WebhookDelivery.retry_after <= now,
        ).all()

        count = {"retried": 0, "success": 0, "failed": 0}

        for delivery in pending:
            try:
                success, status_code, response_body, response_data = await self.webhook_sender.send(
                    url=delivery.url,
                    payload=delivery.payload,
                )

                if success:
                    delivery.status = "sent"
                    delivery.response_code = status_code
                    delivery.response_body = response_body
                    delivery.delivered_at = now
                    count["success"] += 1
                else:
                    delivery.status = "failed"
                    delivery.response_code = status_code
                    delivery.response_body = response_body

                    if self._should_retry_webhook(delivery):
                        await self._schedule_webhook_retry(delivery)
                    else:
                        count["failed"] += 1

                count["retried"] += 1

            except Exception as e:
                logger.error(f"Error retrying webhook {delivery.id}: {e}")
                delivery.status = "failed"
                count["failed"] += 1
                count["retried"] += 1

        self.db.commit()
        return count

    def check_user_preferences(
        self,
        user_id: UUID,
        notification_type: str,
        category: str,
    ) -> bool:
        prefs = self.db.query(NotificationPreference).filter(
            NotificationPreference.user_id == user_id
        ).first()

        if not prefs:
            return True

        if notification_type == "email" and not prefs.email_enabled:
            return False

        if notification_type == "sms" and not prefs.sms_enabled:
            return False

        category_map = {
            "application_status": prefs.application_status_enabled,
            "payment_reminder": prefs.payment_reminders_enabled,
            "statement": prefs.statements_enabled,
            "urgent": prefs.urgent_notifications_enabled,
            "marketing": prefs.marketing_enabled,
        }

        if category in category_map:
            return category_map[category]

        return True

    def is_quiet_hours(self, user_id: UUID) -> bool:
        prefs = self.db.query(NotificationPreference).filter(
            NotificationPreference.user_id == user_id
        ).first()

        if not prefs or not prefs.quiet_hours_start or not prefs.quiet_hours_end:
            return False

        now = datetime.utcnow().time()
        start_time = datetime.strptime(prefs.quiet_hours_start, "%H:%M").time()
        end_time = datetime.strptime(prefs.quiet_hours_end, "%H:%M").time()

        if start_time <= end_time:
            return start_time <= now <= end_time
        else:
            return now >= start_time or now <= end_time

    def get_queue_stats(self) -> dict[str, int]:
        stats = self.db.query(Notification.status).all()
        counts = {"queued": 0, "processing": 0, "failed": 0, "retrying": 0}

        for (status,) in stats:
            if status in counts:
                counts[status] += 1

        return counts

    async def shutdown(self):
        await self.webhook_sender.close()