# Notification System

> **2026-07 — Dave-template integration.** The template catalog now carries all
> 47 notification types from Dave's canonical docs (docs/notifications_source/):
> customer lifecycle + servicing, tiered dunning, and 17 internal back-office/
> vendor notices. Key pieces:
> * `app/templates/emails/_base.html` — shared branded layout; global default
>   greeting/signature per Dave's Global Defaults doc.
> * `app/services/notification_render.py` — registry of all types;
>   `_global_context()` auto-injects company fields (support email, URLs).
> * `app/services/notification_internal.py` — internal-notice catalog (all 17
>   render through `internal_notice.html`).
> * `app/services/notification_processor.py` — event wiring incl. loan
>   lifecycle (disbursed/payment/paid-off/charged-off/agreement events).
> * Cockpit config: `GET/PUT /api/v1/admin/config/notification-rules` — full
>   catalog with per-type cadence, channel toggles, Jinja content overrides
>   (validated at save). No seeding required.
> * Coverage: `tests/test_notification_templates.py` renders every type against
>   its sample context (StrictUndefined) from `tests/fixtures/notification_manifests/`.
> See docs/notifications_source/INTEGRATION_STATUS.md for wired-vs-pending.

## Overview

The PaySpyre notification system provides comprehensive email, SMS, and webhook notification capabilities with queue-based async delivery, retry logic, and delivery tracking.

## Features

- **Multi-channel support**: Email (Resend), SMS (Twilio), Webhooks
- **Queue-based processing**: Async delivery with background task processing
- **Retry logic**: Configurable retry attempts with exponential backoff
- **Delivery tracking**: Full audit trail of all notification attempts
- **User preferences**: Per-user notification preferences and quiet hours
- **Template system**: Reusable email and SMS templates with Jinja2
- **Bulk notifications**: Send to multiple recipients efficiently
- **Webhook integration**: Vendor webhook delivery with retry support

## Architecture

### Core Components

1. **Models** (`app/models/notification.py`)
   - `NotificationTemplate`: Reusable notification templates
   - `Notification`: Queued notifications with status tracking
   - `Delivery`: Individual delivery attempts with provider responses
   - `NotificationPreference`: User notification preferences
   - `WebhookDelivery`: Vendor webhook delivery tracking

2. **Services** (`app/services/notifications.py`)
   - `EmailSender`: Resend email integration
   - `SmsSender`: Twilio SMS integration
   - `WebhookSender`: HTTP webhook delivery
   - `TemplateRenderer`: Jinja2 template rendering
   - `NotificationQueue`: Queue management and processing

3. **API Endpoints** (`app/api/v1/endpoints/notifications.py`)
   - Template CRUD operations
   - Notification queue management
   - Bulk notification sending
   - Preference management
   - Webhook delivery and retry

4. **Templates** (`app/templates/`)
   - Email: `emails/` directory with HTML templates
   - SMS: `sms/` directory with text templates

## Configuration

Add to `.env`:

```bash
# Email (Resend)
RESEND_API_KEY=your_resend_api_key
RESEND_FROM_EMAIL=noreply@payspyre.com

# SMS (Twilio)
TWILIO_ACCOUNT_SID=your_twilio_account_sid
TWILIO_AUTH_TOKEN=your_twilio_auth_token
TWILIO_FROM_NUMBER=+1234567890

# Queue Settings
NOTIFICATION_QUEUE_PROCESSING_INTERVAL=60
NOTIFICATION_MAX_RETRIES=3
NOTIFICATION_RETRY_DELAYS=5,15,30

# Webhook Settings
WEBHOOK_TIMEOUT_SECONDS=30
WEBHOOK_MAX_RETRIES=3
```

## API Endpoints

### Templates

- `POST /api/v1/notifications/templates` - Create template
- `GET /api/v1/notifications/templates` - List templates
- `GET /api/v1/notifications/templates/{id}` - Get template
- `PUT /api/v1/notifications/templates/{id}` - Update template
- `DELETE /api/v1/notifications/templates/{id}` - Delete template

### Queue Management

- `POST /api/v1/notifications/queue` - Queue single notification
- `POST /api/v1/notifications/queue/bulk` - Queue bulk notifications
- `GET /api/v1/notifications/queue/stats` - Get queue statistics
- `POST /api/v1/notifications/queue/process` - Process queue
- `POST /api/v1/notifications/queue/retry` - Retry failed notifications

### Notifications

- `GET /api/v1/notifications` - List notifications
- `GET /api/v1/notifications/{id}` - Get notification
- `PUT /api/v1/notifications/{id}` - Update notification
- `DELETE /api/v1/notifications/{id}` - Delete notification
- `GET /api/v1/notifications/{id}/deliveries` - Get delivery history

### Preferences

- `POST /api/v1/notifications/preferences` - Create preferences
- `GET /api/v1/notifications/preferences/{user_id}` - Get preferences
- `PUT /api/v1/notifications/preferences/{user_id}` - Update preferences
- `GET /api/v1/notifications/users/{user_id}/check-preferences` - Check if notification allowed

### Webhooks

- `POST /api/v1/notifications/webhooks` - Send webhook
- `GET /api/v1/notifications/webhooks/{id}` - Get webhook delivery
- `GET /api/v1/notifications/webhooks` - List webhook deliveries
- `POST /api/v1/notifications/webhooks/retry` - Retry pending webhooks

## Usage Examples

### Send Email Notification

```python
from app.services.notifications import NotificationQueue
from app.db.base import SessionLocal

db = SessionLocal()
queue = NotificationQueue(db)

notification_data = {
    "type": "email",
    "recipient": "user@example.com",
    "subject": "Application Approved",
    "body": "<html>...</html>",
    "priority": "normal",
}

notification = await queue.queue_notification(notification_data)
await queue.process_queue()
```

### Send SMS Notification

```python
notification_data = {
    "type": "sms",
    "recipient": "+1234567890",
    "body": "Your payment is due in 3 days.",
    "priority": "high",
}

notification = await queue.queue_notification(notification_data)
```

### Use Email Template

```python
renderer = TemplateRenderer()
subject, html = renderer.render(
    template_name="application_approved",
    variables={
        "borrower_name": "John Doe",
        "amount": "$10,000",
        "application_id": "abc-123",
        "agreement_url": "https://...",
    }
)

notification_data = {
    "type": "email",
    "recipient": "user@example.com",
    "subject": subject,
    "body": html,
}
```

### Check User Preferences

```python
allowed = queue.check_user_preferences(
    user_id=uuid,
    notification_type="email",
    category="application_status",
)

if allowed and not queue.is_quiet_hours(user_id=uuid):
    # Send notification
    pass
```

### Send Vendor Webhook

```python
success, delivery_id = await queue.send_webhook(
    vendor_id=vendor_uuid,
    event_type="application.approved",
    url="https://vendor.example.com/webhook",
    payload={
        "application_id": str(app_uuid),
        "status": "approved",
        "amount": 10000,
    },
)
```

## Template Variables

### Application Approved
- `borrower_name`
- `amount`
- `application_id`
- `interest_rate`
- `term`
- `monthly_payment`
- `agreement_url`

### Payment Reminder
- `borrower_name`
- `loan_id`
- `payment_amount`
- `due_date`
- `days_until_due`
- `payment_url`
- `late_fee`
- `payment_method`

### Monthly Statement
- `borrower_name`
- `statement_period`
- `loan_id`
- `account_number`
- `current_balance`
- `principal_balance`
- `interest_accrued`
- `next_payment`
- `next_due_date`
- `total_paid_ytd`
- `transactions` (list)

### Urgent Action Required
- `borrower_name`
- `alert_title`
- `alert_message`
- `deadline`
- `time_remaining`
- `reference_id`
- `loan_application_id`
- `action_steps` (list)
- `consequences` (list)
- `action_url`

## Retry Logic

Notifications are retried based on priority:

- **High/Urgent**: Always retry up to max attempts
- **Normal/Low**: Retry up to 3 times

Retry delays (default): 5 min, 15 min, 30 min

Webhooks are retried up to 3 times with delays: 1 min, 5 min, 15 min

## Queue Processing

The queue can be processed:

1. **On-demand**: Call `POST /api/v1/notifications/queue/process`
2. **Scheduled**: Set up a cron job or background task
3. **Background task**: Automatically triggered when queuing notifications

Example cron job:
```bash
*/1 * * * * curl -X POST http://localhost:8000/api/v1/notifications/queue/process
```

## Monitoring

### Queue Statistics

```bash
GET /api/v1/notifications/queue/stats
```

Response:
```json
{
  "queued_count": 15,
  "processing_count": 3,
  "failed_count": 2,
  "retry_count": 5
}
```

### Delivery Tracking

Each notification has full delivery history:

```bash
GET /api/v1/notifications/{id}/deliveries
```

Response includes:
- Attempt number
- Status (pending/sent/delivered/failed)
- Provider (resend/twilio)
- Provider message ID
- Response data
- Error details

## Best Practices

1. **Use templates**: Create reusable templates for consistent messaging
2. **Check preferences**: Always verify user preferences before sending
3. **Respect quiet hours**: Don't send non-urgent notifications during quiet hours
4. **Set appropriate priority**: Use priority levels for queue ordering
5. **Monitor failures**: Regularly check failed notifications and webhooks
6. **Handle webhooks gracefully**: Always implement retry logic for vendor webhooks
7. **Keep SMS short**: SMS messages should be concise (160 chars ideal)
8. **Use clear subjects**: Email subjects should be descriptive and actionable

## Security

- All API keys stored in environment variables
- Webhook URLs validated before delivery
- User preferences enforce opt-in for marketing
- Sensitive data never logged in plain text

## Troubleshooting

### Emails not sending

1. Check `RESEND_API_KEY` is set
2. Verify `RESEND_FROM_EMAIL` is verified in Resend
3. Check delivery logs for error messages

### SMS not sending

1. Check `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN`
2. Verify `TWILIO_FROM_NUMBER` is valid
3. Ensure recipient phone number format is correct (+countrycode)

### Queue not processing

1. Check queue stats endpoint
2. Verify scheduled tasks are running
3. Check for database connection issues

### Webhooks failing

1. Verify webhook URL is accessible
2. Check vendor server is responding
3. Review response codes and error messages
4. Ensure payload format matches vendor expectations

## Migration

Run migration to create notification tables:

```bash
alembic upgrade head
```

This creates:
- `notification_templates`
- `notifications`
- `deliveries`
- `notification_preferences`
- `webhook_deliveries`