# Stripe Integration

## Overview

The PaySpyre backend integrates with Stripe for payment processing and vendor disbursements via Stripe Connect.

## Configuration

Set the following environment variables:

```bash
STRIPE_SECRET_KEY=sk_test_your_stripe_secret_key  # or sk_live_ for production
STRIPE_WEBHOOK_SECRET=whsec_your_webhook_secret  # Get from Stripe dashboard
```

## Installation

Stripe SDK is included in dependencies:
```bash
pip install stripe
```

## Database Migration

Run the migration to create Stripe tables:

```bash
alembic upgrade head
```

This creates:
- `payment_methods` - Patient payment methods (cards, bank accounts)
- `stripe_accounts` - Vendor Stripe Connect accounts
- `stripe_transactions` - All Stripe transactions
- `stripe_webhook_events` - Webhook event logs
- `stripe_payouts` - Vendor payout history

## API Endpoints

### Customer Management

#### Create Stripe Customer
```http
POST /api/v1/stripe/customers
Content-Type: application/json

{
  "borrower_id": "uuid",
  "email": "patient@example.com",
  "name": "John Doe",
  "phone": "+1234567890"
}
```

### Payment Methods

#### Register Payment Method
```http
POST /api/v1/stripe/payment-methods/register
Content-Type: application/json

{
  "payment_method_type": "card",
  "token": "pm_card_visa",
  "is_default": false
}
```

#### List Patient Payment Methods
```http
GET /api/v1/stripe/borrowers/{borrower_id}/payment-methods
```

#### Create Setup Intent (for saving payment methods)
```http
POST /api/v1/stripe/setup-intents
Content-Type: application/json

{
  "customer_id": "cus_xxx",
  "payment_method_types": ["card", "us_bank_account"]
}
```

### Payment Processing

#### Create Payment Intent
```http
POST /api/v1/stripe/payment-intents
Content-Type: application/json

{
  "amount": 150.00,
  "currency": "cad",
  "customer_id": "cus_xxx",
  "payment_method_id": "pm_xxx",
  "application_id": "uuid",
  "confirm": false,
  "off_session": false
}
```

#### Confirm Payment Intent
```http
POST /api/v1/stripe/payment-intents/{payment_intent_id}/confirm
Content-Type: application/json

{
  "payment_method_id": "pm_xxx"
}
```

### Vendor Stripe Connect

#### Create Stripe Connect Account
```http
POST /api/v1/stripe/vendors/{vendor_id}/stripe-accounts
Content-Type: application/json

{
  "vendor_id": "uuid",
  "stripe_account_type": "express",
  "default_payout_schedule": "manual"
}
```

#### Generate Onboarding Link
```http
POST /api/v1/stripe/stripe-accounts/{stripe_account_id}/onboarding
Content-Type: application/json

{
  "refresh_url": "https://payspyre.com/vendors/onboarding/refresh",
  "return_url": "https://payspyre.com/vendors/onboarding/complete"
}
```

#### Get Vendor Stripe Account
```http
GET /api/v1/stripe/vendors/{vendor_id}/stripe-account
```

### Disbursements

#### Create Disbursement to Vendor
```http
POST /api/v1/stripe/disbursements
Content-Type: application/json

{
  "amount": 1500.00,
  "vendor_stripe_account_id": "acct_xxx",
  "application_id": "uuid",
  "reference_number": "FND-ABC123"
}
```

### Payouts

#### Create Payout from Vendor Account
```http
POST /api/v1/stripe/payouts
Content-Type: application/json

{
  "stripe_account_id": "uuid",
  "amount": 1450.00,
  "currency": "cad"
}
```

#### List Vendor Payouts
```http
GET /api/v1/stripe/stripe-accounts/{stripe_account_id}/payouts
```

### Refunds

#### Create Refund
```http
POST /api/v1/stripe/refunds
Content-Type: application/json

{
  "payment_intent_id": "pi_xxx",
  "amount": 50.00,
  "reason": "requested_by_customer"
}
```

### Balance

#### Get Platform Balance
```http
GET /api/v1/stripe/balance
```

#### Get Vendor Balance
```http
GET /api/v1/stripe/vendors/{vendor_id}/balance
```

### Transactions & Webhooks

#### List Transactions
```http
GET /api/v1/stripe/transactions?application_id=uuid&transaction_type=payment&status=succeeded
```

#### List Webhook Events
```http
GET /api/v1/stripe/webhook-events?processed=false&event_type=payment_intent.succeeded
```

#### Webhook Endpoint
```http
POST /api/v1/stripe/webhook
Stripe-Signature: <signature>
Content-Type: application/json

{
  "id": "evt_xxx",
  "type": "payment_intent.succeeded",
  ...
}
```

## Webhook Events

The following Stripe webhook events are processed:

- `payment_intent.succeeded` - Updates payment status
- `payment_intent.payment_failed` - Records failure and updates status
- `transfer.paid` - Marks disbursement as completed
- `transfer.failed` - Records disbursement failure
- `payout.created` - Logs new payout
- `payout.paid` - Updates payout status
- `payout.failed` - Records payout failure
- `charge.refunded` - Records refund transaction
- `account.updated` - Syncs account verification status

## Data Model

### PaymentMethod
- Links borrower to Stripe payment methods
- Stores card/bank account details
- Tracks verification and default status

### StripeAccount
- Vendor Stripe Connect account
- Tracks onboarding progress
- Manages payout settings

### StripeTransaction
- Records all Stripe transactions
- Links to application, payment, refund
- Tracks fees and transfer groups

### StripeWebhookEvent
- Logs all incoming webhooks
- Tracks processing status
- Links to related transactions

### StripePayout
- Vendor payout history
- Tracks status and arrival dates
- Records failure details

## Integration with Funding Flow

When funding a loan application:

1. Create Stripe Connect account for vendor (once)
2. Vendor completes onboarding via Stripe UI
3. Verify account has `charges_enabled` and `payouts_enabled`
4. Call `/api/v1/stripe/disbursements` to transfer funds
5. Webhook updates funding status on success/failure

## Payment Processing Flow

1. Patient adds payment method via `/api/v1/stripe/payment-methods/register`
2. Create payment intent via `/api/v1/stripe/payment-intents`
3. Confirm payment intent via `/api/v1/stripe/payment-intents/{id}/confirm`
4. Webhook processes `payment_intent.succeeded` event
5. Payment record updated with transaction ID
6. Loan balance recalculated

## Security Notes

- Stripe secret key never exposed to frontend
- Webhook signature verified for all events
- Customer IDs stored per borrower for isolation
- All Stripe data logged in `stripe_response` JSON column

## Testing

Use Stripe test mode for development:
- Test cards: https://stripe.com/docs/testing
- Test bank accounts: https://stripe.com/docs/testing#bank-accounts
- Test webhooks: Use Stripe CLI or ngrok for local testing

```bash
# Stripe CLI webhook forwarding
stripe listen --forward-to localhost:8000/api/v1/stripe/webhook
```

## Production Checklist

- [ ] Switch from `sk_test_` to `sk_live_` secret key
- [ ] Set production webhook secret
- [ ] Enable Radar fraud rules
- [ ] Set up automated payouts for vendors
- [ ] Configure payout schedule preferences
- [ ] Set up 1099-K reporting for US vendors
- [ ] Enable Stripe Email receipts
- [ ] Configure Stripe Dashboard alerts
- [ ] Set up webhook retry monitoring
- [ ] Review and update tax settings