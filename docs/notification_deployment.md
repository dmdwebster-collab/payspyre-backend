# Notification System Deployment Guide

## Prerequisites

1. PaySpyre backend already deployed
2. Database migration completed
3. Environment variables configured

## Installation

### 1. Install Dependencies

```bash
pip install resend twilio jinja2
```

Or update from requirements:

```bash
pip install -e .
```

### 2. Configure Environment Variables

Add to your `.env` file:

```bash
# Email (Resend)
RESEND_API_KEY=re_xxxxxxxxxxxx
RESEND_FROM_EMAIL=noreply@payspyre.com

# SMS (Twilio)
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_FROM_NUMBER=+1234567890

# Queue Settings
NOTIFICATION_QUEUE_PROCESSING_INTERVAL=60
NOTIFICATION_MAX_RETRIES=3
NOTIFICATION_RETRY_DELAYS=5,15,30

# Webhook Settings
WEBHOOK_TIMEOUT_SECONDS=30
WEBHOOK_MAX_RETRIES=3
```

### 3. Run Database Migration

```bash
alembic upgrade head
```

This creates the following tables:
- `notification_templates`
- `notifications`
- `deliveries`
- `notification_preferences`
- `webhook_deliveries`

### 4. Initialize Default Templates

```bash
python scripts/init_notification_templates.py
```

This populates the database with default email and SMS templates.

### 5. Verify Installation

Start the server:

```bash
uvicorn app.main:app --reload
```

Test the health endpoint:

```bash
curl http://localhost:8000/health
```

Check queue stats:

```bash
curl http://localhost:8000/api/v1/notifications/queue/stats
```

## Service Setup

### Option 1: Built-in Background Processing

The notification queue is automatically processed in the background when the server starts. No additional setup needed.

### Option 2: External Cron Job

If you prefer external processing, disable the built-in processor and set up a cron job:

```bash
# crontab -e
*/1 * * * * curl -X POST http://localhost:8000/api/v1/notifications/queue/process
*/5 * * * * curl -X POST http://localhost:8000/api/v1/notifications/webhooks/retry
```

### Option 3: Dedicated Worker Service

For high-volume deployments, run a dedicated worker:

```bash
# Create a separate worker script
python scripts/notification_worker.py
```

## Monitoring

### Queue Monitoring

Check queue statistics:

```bash
curl http://localhost:8000/api/v1/notifications/queue/stats
```

Expected response:

```json
{
  "queued_count": 15,
  "processing_count": 3,
  "failed_count": 2,
  "retry_count": 5
}
```

### Failed Notifications

Monitor failed notifications:

```bash
curl "http://localhost:8000/api/v1/notifications?status=failed&limit=10"
```

### Delivery Tracking

Check delivery history for a specific notification:

```bash
curl http://localhost:8000/api/v1/notifications/{notification_id}/deliveries
```

## Testing

### Test Email Sending

```bash
curl -X POST http://localhost:8000/api/v1/notifications/queue \
  -H "Content-Type: application/json" \
  -d '{
    "type": "email",
    "recipient": "your-email@example.com",
    "subject": "Test Notification",
    "body": "<html><body>Test email from PaySpyre</body></html>",
    "priority": "normal"
  }'
```

### Test SMS Sending

```bash
curl -X POST http://localhost:8000/api/v1/notifications/queue \
  -H "Content-Type: application/json" \
  -d '{
    "type": "sms",
    "recipient": "+1234567890",
    "body": "Test SMS from PaySpyre",
    "priority": "normal"
  }'
```

### Test Webhook Delivery

```bash
curl -X POST http://localhost:8000/api/v1/notifications/webhooks \
  -H "Content-Type: application/json" \
  -d '{
    "vendor_id": "uuid-here",
    "event_type": "test.event",
    "url": "https://webhook.site/your-uuid",
    "payload": {"test": "data"}
  }'
```

## Troubleshooting

### Emails Not Sending

1. Check Resend API key is valid
2. Verify `RESEND_FROM_EMAIL` is verified in Resend dashboard
3. Check server logs for error messages
4. Test Resend API directly:

```bash
curl https://api.resend.com/emails \
  -H "Authorization: Bearer re_xxxxxxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{
    "from": "noreply@payspyre.com",
    "to": ["test@example.com"],
    "subject": "Test",
    "html": "<html>Test</html>"
  }'
```

### SMS Not Sending

1. Verify Twilio credentials
2. Check `TWILIO_FROM_NUMBER` format (+countrycode)
3. Ensure recipient phone number format is correct
4. Check Twilio dashboard for message logs

### Queue Not Processing

1. Verify server is running
2. Check queue stats endpoint
3. Review server logs for errors
4. Ensure database connection is healthy

### High Failure Rate

1. Check provider API status
2. Verify API credentials are valid
3. Review rate limits
4. Check network connectivity
5. Examine delivery logs for patterns

## Production Checklist

- [ ] Environment variables configured
- [ ] Database migration completed
- [ ] Default templates initialized
- [ ] Resend API key verified
- [ ] Twilio credentials verified
- [ ] Queue processing enabled
- [ ] Monitoring set up
- [ ] Error alerts configured
- [ ] Backup procedures tested
- [ ] Documentation reviewed

## Scaling Considerations

### High Volume Email

- Use Resend's bulk API for large sends
- Implement rate limiting
- Consider dedicated email server for very high volumes

### High Volume SMS

- Monitor Twilio usage and costs
- Implement cost controls
- Use short codes for high volume

### Database Performance

- Add indexes on frequently queried columns
- Consider partitioning for large notification tables
- Implement archive strategy for old notifications

## Security

- [ ] API keys stored securely
- [ ] Webhook URLs validated
- [ ] Rate limiting enabled
- [ ] Input validation on all endpoints
- [ ] Sensitive data not logged
- [ ] User preferences respected

## Maintenance

### Daily

- Check queue stats
- Monitor failed notifications
- Review error logs

### Weekly

- Review delivery success rates
- Analyze webhook failures
- Check provider costs

### Monthly

- Archive old notifications
- Review and update templates
- Optimize database performance
- Review user preferences compliance

## Support

For issues or questions:
- Documentation: `docs/notification_system.md`
- API Docs: `/docs` endpoint
- Email: dev@payspyre.com