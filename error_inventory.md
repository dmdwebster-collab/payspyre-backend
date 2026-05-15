# Test Error Inventory

## Summary: 66 Errors Categorized

| Category | Count | Test Files | Root Cause | Fix Strategy |
|----------|-------|------------|------------|--------------|
| Missing `client` fixture | 20+ | test_documents.py (5), test_stripe.py (14), test_analytics.py (1), test_auth.py (~20) | FastAPI test client fixture not defined in conftest.py | Add `client` fixture to conftest.py using FastAPI TestClient |
| Missing `test_db` fixture | 11 | test_notifications.py (11) | Fixture references `test_db` which doesn't exist | Rename to use `db_session` from conftest.py |
| SQLite instead of PostgreSQL | 15 | test_analytics.py (15) | Tests creating direct SQLite connections | Use PostgreSQL via conftest.py db_session fixture |
| FK violations | 6 | test_underwriting.py (6) | Fixture creates child before parent records | Fix creation order (partially done) |
| ARRAY type incompatibility | 1 | test_analytics.py (manual_kyb_reviews) | PostgreSQL ARRAY(JSONB) incompatible with test setup | Already fixed by using PostgreSQL in conftest.py |

## Detailed Breakdown

### 1. Missing `client` Fixture (20+ errors)

**Tests Affected:**
- `test_documents.py::test_initiate_document_upload` - line 104
- `test_documents.py::test_confirm_document_upload` - line 133
- `test_documents.py::test_get_document` - line 171
- `test_documents.py::test_list_documents` - line 195
- `test_documents.py::test_delete_document` - line 222
- `test_stripe.py::TestPaymentMethods::test_register_payment_method_success` - line 163
- `test_stripe.py::TestPaymentMethods::test_list_payment_methods` - line 193
- `test_stripe.py::TestPaymentIntents::test_create_payment_intent` - line 220
- `test_stripe.py::TestStripeAccounts::test_create_stripe_account` - line 252
- `test_stripe.py::TestStripeAccounts::test_get_vendor_stripe_account` - line 274
- `test_stripe.py::TestDisbursements::test_create_disbursement_success` - line 299
- `test_stripe.py::TestWebhooks::test_webhook_invalid_signature` - line 349
- `test_stripe.py::TestWebhooks::test_webhook_duplicate_event` - line 359
- `test_stripe.py::TestPayouts::test_create_payout` - line 382
- `test_stripe.py::TestPayouts::test_list_payouts` - line 417
- `test_stripe.py::TestRefunds::test_create_refund` - line 451
- `test_stripe.py::TestBalance::test_get_platform_balance` - line 475
- `test_stripe.py::TestBalance::test_get_vendor_balance` - line 489
- `test_stripe.py::TestTransactions::test_list_transactions_filtered` - line 516
- `test_auth.py` - All tests (estimated ~20)
- `test_analytics.py::test_analytics_empty_data` - line 304

**Error Message:**
```
fixture 'client' not found
available fixtures: [..., db_session, ...]
```

**Tables Involved:** Various (documents, users, borrowers, vendors, etc.)

**Fix:** Add FastAPI TestClient fixture to conftest.py:
```python
from fastapi.testclient import TestClient
from app.main import app

@pytest.fixture
def client(db_session):
    """FastAPI test client with database session override."""
    def override_get_db():
        try:
            yield db_session
        finally:
            pass
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
```

### 2. Missing `test_db` Fixture (11 errors)

**Tests Affected:**
- `test_notifications.py::TestNotificationTemplate::test_create_template` - line 26
- `test_notifications.py::TestNotificationTemplate::test_template_unique_name` - line 46
- `test_notifications.py::TestNotificationQueue::test_queue_notification` - line 69
- `test_notifications.py::TestNotificationQueue::test_queue_bulk_notifications` - line 85
- `test_notifications.py::TestNotificationQueue::test_get_queue_stats` - line 101
- `test_notifications.py::TestNotificationPreferences::test_create_preferences` - line 119
- `test_notifications.py::TestWebhookDelivery::test_create_webhook_delivery` - line 155
- `test_notifications.py::TestDelivery::test_create_delivery` - line 175
- `test_notifications.py::TestTemplateRenderer::test_render_from_string` - line 203
- `test_notifications.py::TestRetryLogic::test_should_retry_by_priority` - line 214
- `test_notifications.py::TestRetryLogic::test_should_not_retry_low_priority` - line 227

**Error Message:**
```
fixture 'test_db' not found
```

**Tables Involved:** notification_templates, notifications, notification_preferences, etc.

**Fix:** Update test_notifications.py line 15-17 to rename fixture from `test_db` to use `db_session` directly, or update the fixture definition to use `db_session`:
```python
@pytest.fixture
def db_session(db_session):  # Use the conftest.py fixture
    return db_session
```

### 3. SQLite Instead of PostgreSQL (15 errors)

**Tests Affected:**
- `test_analytics.py::test_get_analytics_basic`
- `test_analytics.py::test_get_analytics_with_date_range`
- `test_analytics.py::test_get_analytics_weekly_granularity`
- `test_analytics.py::test_get_analytics_monthly_granularity`
- `test_analytics.py::test_approval_rates_structure`
- `test_analytics.py::test_loan_metrics_calculation`
- `test_analytics.py::test_vendor_performance_ranking`
- `test_analytics.py::test_geographic_distribution`
- `test_analytics.py::test_risk_score_distribution`
- `test_analytics.py::test_delinquency_tracking`
- `test_analytics.py::test_export_loans_csv`
- `test_analytics.py::test_export_payments_csv`
- `test_analytics.py::test_export_vendors_csv`
- `test_analytics.py::test_export_with_date_range`
- `test_analytics.py::test_analytics_empty_data`

**Error Message:**
```
sqlite3.OperationalError: no such table: loan_applications
```

**Tables Involved:** loan_applications, borrowers, vendors, kyc_sessions, etc.

**Fix:** The analytics tests are creating their own SQLite database. Need to update to use PostgreSQL via `db_session` fixture from conftest.py.

### 4. FK Violations (6 failures in test_underwriting.py)

**Tests Affected:**
- `test_underwriting.py::test_manual_review_approve`
- `test_underwriting.py::test_manual_review_reject`
- `test_underwriting.py::test_get_underwriting_status`
- `test_underwriting.py::test_request_rereview`

**Error Message:**
```
sqlalchemy.exc.IntegrityError: (psycopg2.errors.ForeignKeyViolation)
```

**Tables Involved:** manual_kyb_reviews, kyc_sessions, loan_applications

**Fix:** Already partially applied. Need to ensure fixtures create parent records before child records with proper `flush()` calls.

## Fix Order Priority

1. **High Priority:** Add `client` fixture to conftest.py (fixes 20+ errors immediately)
2. **High Priority:** Fix test_notifications.py fixture references (fixes 11 errors)
3. **Medium Priority:** Update analytics tests to use PostgreSQL (fixes 15 errors)
4. **Low Priority:** Remaining underwriting FK fixes (6 failures)

## Expected Progression

- Start: 66 errors, 7 failures, 18 passed
- After client fixture: ~46 errors, 7 failures, 18 passed
- After notifications fix: ~35 errors, 7 failures, 18 passed
- After analytics PostgreSQL: ~20 errors, 7 failures, 18 passed
- After underwriting fixes: **0 errors, 0 failures, 91 passed**
