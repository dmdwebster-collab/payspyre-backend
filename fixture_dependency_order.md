# Fixture Dependency Order

## Foreign Key Dependency Graph

Based on migration schema analysis, here's the table creation order:

### Root Tables (No FK dependencies)
1. `users` - User accounts
2. `roles` - Role definitions
3. `permissions` - Permission definitions
4. `vendors` - Vendor records

### Level 1 (Depend on Root)
5. `user_roles` → FK to `users`, `roles`
6. `role_permissions` → FK to `roles`, `permissions`
7. `borrowers` → FK to `users` (optional)
8. `loan_applications` → FK to `borrowers`, `vendors`

### Level 2 (Depend on Level 1)
9. `kyc_sessions` → FK to `loan_applications`, `borrowers`
10. `documents` → FK to `loan_applications`, `borrowers`, `vendors`
11. `kyc_results` → FK to `kyc_sessions`

### Level 3 (Depend on Level 2)
12. `document_versions` → FK to `documents`
13. `manual_kyb_reviews` → FK to `vendors`, `users`
14. `credit_inquiries` → FK to `borrowers`, `loan_applications`
15. `credit_reports` → FK to `credit_inquiries`

### Level 4 (Depend on Level 1-3)
16. `payment_methods` → FK to `borrowers`
17. `stripe_accounts` → FK to `vendors`
18. `stripe_transactions` → FK to `payment_methods`, `stripe_accounts`, `loan_applications`
19. `notifications` → FK to `users`, `loan_applications`
20. `notification_preferences` → FK to `users`
21. `notification_templates` - No FKs
22. `deliveries` → FK to `notifications`
23. `webhook_deliveries` → FK to `notifications`
24. `stripe_payouts` → FK to `stripe_accounts`
25. `stripe_webhook_events` → FK to `stripe_transactions`

26. `api_keys` → FK to `users`
27. `sessions` → FK to `users`

## Test Fixture Creation Pattern

### Canonical Pattern (from underwriting test fix)

```python
@pytest.fixture
def setup_complex_data(db_session):
    # Step 1: Create root entities (no FK dependencies)
    vendor = Vendor(id=uuid4(), business_name="Test", ...)
    db_session.add(vendor)
    
    borrower = Borrower(id=uuid4(), first_name="John", ...)
    db_session.add(borrower)
    
    # Step 2: Create Level 1 entities (depend on root)
    application = LoanApplication(
        id=uuid4(),
        borrower_id=borrower.id,  # FK to borrower
        vendor_id=vendor.id,        # FK to vendor
        ...
    )
    db_session.add(application)
    
    # Step 3: Flush to ensure FK references exist in database
    db_session.flush()
    
    # Step 4: Create Level 2 entities (depend on Level 1)
    kyc_session = KycSession(
        id=uuid4(),
        loan_application_id=application.id,  # FK to application
        borrower_id=borrower.id,
        ...
    )
    db_session.add(kyc_session)
    
    # Step 5: Create Level 3 entities (depend on Level 2)
    kyc_result = KycResult(
        kyc_session_id=kyc_session.id,  # FK to kyc_session
        ...
    )
    db_session.add(kyc_result)
    
    # Step 6: Commit all at once
    db_session.commit()
    
    return {
        "vendor": vendor,
        "borrower": borrower,
        "application": application,
        "kyc_session": kyc_session,
        "kyc_result": kyc_result,
    }
```

## Key Rules

1. **Always add to session before flushing** - `db_session.add()` first, then `db_session.flush()`
2. **Flush before creating FK-dependent records** - This ensures parent IDs exist in the database
3. **Create in dependency order** - Root → Level 1 → Level 2 → Level 3
4. **Use the same UUID** - If you need the ID later, store it in a variable and reuse
5. **Commit once at the end** - For fixture setup, commit all records together

## Common Mistakes to Avoid

❌ **Wrong:** Creating child before parent
```python
# This fails with FK violation
kyc_session = KycSession(loan_application_id=uuid4(), ...)
db_session.add(kyc_session)
db_session.commit()

application = LoanApplication(id=kyc_session.loan_application_id, ...)
db_session.add(application)
db_session.commit()  # Fails - kyc_session references non-existent application
```

✅ **Right:** Create parent first, flush, then child
```python
application = LoanApplication(id=uuid4(), ...)
db_session.add(application)
db_session.flush()  # Application now exists in DB

kyc_session = KycSession(loan_application_id=application.id, ...)
db_session.add(kyc_session)
db_session.commit()  # Success - both records committed
```

❌ **Wrong:** Using random UUID for FK without creating parent
```python
kyc_session = KycSession(
    loan_application_id=uuid4(),  # Random UUID - no such application exists!
    ...
)
```

✅ **Right:** Create parent first, use its ID
```python
application = LoanApplication(id=uuid4(), ...)
db_session.add(application)
db_session.flush()

kyc_session = KycSession(
    loan_application_id=application.id,  # ID of actual application
    ...
)
```

## Fixture Dependencies for Each Test File

| Test File | Fixtures Needed | Required Fixtures |
|-----------|-----------------|-------------------|
| test_analytics.py | client, db_session | Add `client` fixture, use PostgreSQL |
| test_auth.py | client, db_session | Add `client` fixture |
| test_documents.py | client, db_session | Add `client` fixture, fix FK order |
| test_kyc.py | db_session | Already working ✅ |
| test_notifications.py | db_session | Rename `test_db` to `db_session` |
| test_stripe.py | client, test_*, db_session | Add `client` fixture |
| test_underwriting.py | db_session | Fix FK order (partially done) |
