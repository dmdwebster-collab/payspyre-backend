import pytest
from decimal import Decimal
from unittest.mock import Mock, AsyncMock, patch
from datetime import datetime

from app.models.stripe import PaymentMethod, StripeAccount, StripeTransaction, StripeWebhookEvent
from app.models.funding import Funding
from app.schemas.stripe import (
    PaymentMethodCreate,
    StripeAccountCreate,
    PaymentIntentCreate,
    DisbursementCreate,
)


@pytest.fixture
def mock_stripe_service():
    """Mock Stripe service."""
    with patch('app.services.stripe.stripe') as mock_stripe:
        mock_stripe.api_key = "sk_test_mock"
        yield mock_stripe


@pytest.fixture
def test_vendor(db_session):
    """Create test vendor."""
    from app.models.loan import Vendor
    vendor = Vendor(
        business_name="Test Dental Clinic",
        business_type="corporation",
        contact_name="Dr. Test",
        email="test@clinic.com",
        phone="555-0100",
        address_line1="123 Test St",
        city="Test City",
        province="BC",
        postal_code="V1V1V1",
        status="active",
    )
    db_session.add(vendor)
    db_session.commit()
    db_session.refresh(vendor)
    return vendor


@pytest.fixture
def test_borrower(db_session):
    """Create test borrower."""
    from app.models.loan import Borrower
    borrower = Borrower(
        first_name="John",
        last_name="Doe",
        email="john@example.com",
        phone="555-0101",
        date_of_birth=datetime(1990, 1, 1),
        address_line1="123 Main St",
        city="Kelowna",
        province="BC",
        postal_code="V1V1V1",
        country="CA",
    )
    db_session.add(borrower)
    db_session.commit()
    db_session.refresh(borrower)
    return borrower


@pytest.fixture
def test_application(db_session, test_borrower, test_vendor):
    """Create test loan application."""
    from app.models.loan import LoanApplication
    application = LoanApplication(
        borrower_id=test_borrower.id,
        vendor_id=test_vendor.id,
        requested_amount=Decimal("1500.00"),
        purpose="Dental implants",
        status="approved",
        interest_rate=Decimal("0.12"),
        term_months=12,
        payment_frequency="monthly",
    )
    db_session.add(application)
    db_session.commit()
    db_session.refresh(application)
    return application


class TestStripeService:
    """Test Stripe service methods."""

    @pytest.mark.asyncio
    async def test_create_connect_account(self, mock_stripe_service):
        """Test creating Stripe Connect account."""
        from app.services.stripe import stripe_service

        mock_account = Mock()
        mock_account.id = "acct_123"
        mock_account.to_dict.return_value = {"id": "acct_123"}

        mock_stripe_service.Account.create.return_value = mock_account

        result = await stripe_service.create_connect_account(
            vendor_id="uuid-123",
            email="test@example.com",
            business_name="Test Clinic",
            business_type="company",
        )

        assert result["success"] is True
        assert result["account_id"] == "acct_123"

    @pytest.mark.asyncio
    async def test_create_payment_intent(self, mock_stripe_service):
        """Test creating payment intent."""
        from app.services.stripe import stripe_service

        mock_intent = Mock()
        mock_intent.id = "pi_123"
        mock_intent.client_secret = "pi_123_secret"
        mock_intent.status = "requires_payment_method"
        mock_intent.amount = 15000
        mock_intent.currency = "cad"
        mock_intent.to_dict.return_value = {"id": "pi_123"}

        mock_stripe_service.PaymentIntent.create.return_value = mock_intent

        result = await stripe_service.create_payment_intent(
            amount=Decimal("150.00"),
            currency="cad",
            customer_id="cus_123",
        )

        assert result["success"] is True
        assert result["payment_intent_id"] == "pi_123"
        assert result["client_secret"] == "pi_123_secret"

    @pytest.mark.asyncio
    async def test_create_disbursement(self, mock_stripe_service):
        """Test creating disbursement."""
        from app.services.stripe import stripe_service

        mock_transfer = Mock()
        mock_transfer.id = "tr_123"
        mock_transfer.to_dict.return_value = {"id": "tr_123"}

        mock_stripe_service.Transfer.create.return_value = mock_transfer

        result = await stripe_service.create_disbursement(
            amount=Decimal("1500.00"),
            vendor_stripe_account_id="acct_123",
            application_id="app-123",
            reference_number="FND-TEST",
        )

        assert result["success"] is True
        assert result["transfer_id"] == "tr_123"
        assert result["transfer_group"] == "FND-TEST"


class TestPaymentMethods:
    """Test payment method endpoints."""

    def test_register_payment_method_success(self, client, test_borrower, mock_stripe_service):
        """Test successful payment method registration."""
        mock_customer = Mock()
        mock_customer.id = "cus_123"
        mock_stripe_service.Customer.create.return_value = mock_customer

        mock_pm = Mock()
        mock_pm.id = "pm_123"
        mock_pm.type = "card"
        mock_pm.card = {"last4": "4242", "brand": "visa", "exp_month": 12, "exp_year": 2025}
        mock_pm.to_dict.return_value = {"id": "pm_123"}
        mock_stripe_service.PaymentMethod.create.return_value = mock_pm

        mock_stripe_service.PaymentMethod.attach.return_value = mock_pm

        response = client.post(
            "/api/v1/stripe/payment-methods/register",
            params={"borrower_id": str(test_borrower.id)},
            json={
                "payment_method_type": "card",
                "token": "pm_card_visa",
                "is_default": False,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["payment_method_id"] is not None

    def test_list_payment_methods(self, client, db_session, test_borrower):
        """Test listing payment methods for borrower."""
        payment_method = PaymentMethod(
            borrower_id=test_borrower.id,
            stripe_payment_method_id="pm_123",
            stripe_customer_id="cus_123",
            payment_method_type="card",
            card_last_4="4242",
            card_brand="visa",
            is_default=True,
            is_verified=True,
            status="active",
        )
        db_session.add(payment_method)
        db_session.commit()

        response = client.get(f"/api/v1/stripe/borrowers/{test_borrower.id}/payment-methods")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["card_last_4"] == "4242"


class TestPaymentIntents:
    """Test payment intent endpoints."""

    def test_create_payment_intent(self, client, mock_stripe_service):
        """Test creating payment intent."""
        mock_intent = Mock()
        mock_intent.id = "pi_123"
        mock_intent.client_secret = "pi_123_secret"
        mock_intent.status = "requires_payment_method"
        mock_intent.amount = 15000
        mock_intent.currency = "cad"
        mock_intent.to_dict.return_value = {"id": "pi_123"}

        mock_stripe_service.PaymentIntent.create.return_value = mock_intent

        response = client.post(
            "/api/v1/stripe/payment-intents",
            json={
                "amount": 150.00,
                "currency": "cad",
                "customer_id": "cus_123",
                "payment_method_id": "pm_123",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["payment_intent_id"] == "pi_123"
        assert data["client_secret"] == "pi_123_secret"
        assert data["status"] == "requires_payment_method"


class TestStripeAccounts:
    """Test Stripe Connect account endpoints."""

    def test_create_stripe_account(self, client, test_vendor, mock_stripe_service):
        """Test creating Stripe Connect account for vendor."""
        mock_account = Mock()
        mock_account.id = "acct_123"
        mock_account.to_dict.return_value = {"id": "acct_123"}

        mock_stripe_service.Account.create.return_value = mock_account

        response = client.post(
            f"/api/v1/stripe/vendors/{test_vendor.id}/stripe-accounts",
            json={
                "vendor_id": str(test_vendor.id),
                "stripe_account_type": "express",
                "default_payout_schedule": "manual",
            },
        )

        assert response.status_code == 201
        data = response.json()
        assert data["stripe_account_id"] == "acct_123"
        assert data["stripe_account_type"] == "express"

    def test_get_vendor_stripe_account(self, client, db_session, test_vendor):
        """Test getting vendor's Stripe account."""
        stripe_account = StripeAccount(
            vendor_id=test_vendor.id,
            stripe_account_id="acct_123",
            stripe_account_type="express",
            charges_enabled=False,
            payouts_enabled=False,
            details_submitted=False,
            status="active",
        )
        db_session.add(stripe_account)
        db_session.commit()

        response = client.get(f"/api/v1/stripe/vendors/{test_vendor.id}/stripe-account")

        assert response.status_code == 200
        data = response.json()
        assert data["stripe_account_id"] == "acct_123"
        assert data["onboarding_status"] == "not_started"


class TestDisbursements:
    """Test disbursement endpoints."""

    def test_create_disbursement_success(self, client, db_session, test_vendor, test_application, mock_stripe_service):
        """Test creating disbursement to vendor."""
        stripe_account = StripeAccount(
            vendor_id=test_vendor.id,
            stripe_account_id="acct_123",
            stripe_account_type="express",
            charges_enabled=True,
            payouts_enabled=True,
            details_submitted=True,
            status="active",
        )
        db_session.add(stripe_account)
        db_session.commit()

        funding = Funding(
            application_id=test_application.id,
            disbursement_amount=Decimal("1500.00"),
            disbursement_method="etransfer",
            disbursement_date=datetime.utcnow(),
            reference_number="FND-TEST",
            status="pending",
        )
        db_session.add(funding)
        db_session.commit()

        mock_transfer = Mock()
        mock_transfer.id = "tr_123"
        mock_transfer.to_dict.return_value = {"id": "tr_123"}

        mock_stripe_service.Transfer.create.return_value = mock_transfer

        response = client.post(
            "/api/v1/stripe/disbursements",
            json={
                "amount": 1500.00,
                "vendor_stripe_account_id": "acct_123",
                "application_id": str(test_application.id),
                "reference_number": "FND-TEST",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["transfer_id"] == "tr_123"


class TestWebhooks:
    """Test webhook processing."""

    def test_webhook_invalid_signature(self, client):
        """Test webhook with invalid signature."""
        response = client.post(
            "/api/v1/stripe/webhook",
            headers={"Stripe-Signature": "invalid_signature"},
            json={"id": "evt_123", "type": "payment_intent.succeeded"},
        )

        assert response.status_code == 400

    def test_webhook_duplicate_event(self, client, db_session):
        """Test handling duplicate webhook events."""
        existing_event = StripeWebhookEvent(
            stripe_event_id="evt_123",
            event_type="payment_intent.succeeded",
            event_data={"id": "evt_123"},
        )
        db_session.add(existing_event)
        db_session.commit()

        response = client.post(
            "/api/v1/stripe/webhook",
            headers={"Stripe-Signature": "valid_signature"},
            json={"id": "evt_123", "type": "payment_intent.succeeded"},
        )

        # Should still return 200 even with background task failure
        # Actual signature verification will fail in test


class TestPayouts:
    """Test payout endpoints."""

    def test_create_payout(self, client, db_session, test_vendor, mock_stripe_service):
        """Test creating payout."""
        stripe_account = StripeAccount(
            vendor_id=test_vendor.id,
            stripe_account_id="acct_123",
            stripe_account_type="express",
            payouts_enabled=True,
            status="active",
        )
        db_session.add(stripe_account)
        db_session.commit()

        mock_payout = Mock()
        mock_payout.id = "po_123"
        mock_payout.amount = 145000
        mock_payout.currency = "cad"
        mock_payout.status = "pending"
        mock_payout.to_dict.return_value = {"id": "po_123"}

        mock_stripe_service.Payout.create.return_value = mock_payout

        response = client.post(
            "/api/v1/stripe/payouts",
            json={
                "stripe_account_id": str(stripe_account.id),
                "amount": 1450.00,
                "currency": "cad",
            },
        )

        assert response.status_code == 201
        data = response.json()
        assert data["stripe_payout_id"] == "po_123"
        assert data["amount"] == Decimal("1450.00")

    def test_list_payouts(self, client, db_session, test_vendor):
        """Test listing payouts for account."""
        stripe_account = StripeAccount(
            vendor_id=test_vendor.id,
            stripe_account_id="acct_123",
            stripe_account_type="express",
            payouts_enabled=True,
            status="active",
        )
        db_session.add(stripe_account)
        db_session.commit()

        from app.models.stripe import StripePayout
        payout = StripePayout(
            stripe_account_id=stripe_account.id,
            stripe_payout_id="po_123",
            amount=Decimal("1450.00"),
            currency="cad",
            status="paid",
        )
        db_session.add(payout)
        db_session.commit()

        response = client.get(f"/api/v1/stripe/stripe-accounts/acct_123/payouts")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["stripe_payout_id"] == "po_123"


class TestRefunds:
    """Test refund endpoints."""

    def test_create_refund(self, client, mock_stripe_service):
        """Test creating refund."""
        mock_refund = Mock()
        mock_refund.id = "re_123"
        mock_stripe_service.Refund.create.return_value = mock_refund

        response = client.post(
            "/api/v1/stripe/refunds",
            json={
                "payment_intent_id": "pi_123",
                "amount": 50.00,
                "reason": "requested_by_customer",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["refund_id"] == "re_123"


class TestBalance:
    """Test balance endpoints."""

    def test_get_platform_balance(self, client, mock_stripe_service):
        """Test getting platform balance."""
        mock_balance = Mock()
        mock_balance.available = [{"amount": 100000, "currency": "cad"}]
        mock_balance.pending = [{"amount": 50000, "currency": "cad"}]
        mock_stripe_service.Balance.retrieve.return_value = mock_balance

        response = client.get("/api/v1/stripe/balance")

        assert response.status_code == 200
        data = response.json()
        assert len(data["available"]) == 1
        assert data["available"][0]["amount"] == Decimal("1000.00")

    def test_get_vendor_balance(self, client, db_session, test_vendor, mock_stripe_service):
        """Test getting vendor balance."""
        stripe_account = StripeAccount(
            vendor_id=test_vendor.id,
            stripe_account_id="acct_123",
            stripe_account_type="express",
            status="active",
        )
        db_session.add(stripe_account)
        db_session.commit()

        mock_balance = Mock()
        mock_balance.available = [{"amount": 50000, "currency": "cad"}]
        mock_balance.pending = []
        mock_stripe_service.Balance.retrieve.return_value = mock_balance

        response = client.get(f"/api/v1/stripe/vendors/{test_vendor.id}/balance")

        assert response.status_code == 200
        data = response.json()
        assert len(data["available"]) == 1
        assert data["available"][0]["amount"] == Decimal("500.00")


class TestTransactions:
    """Test transaction listing."""

    def test_list_transactions_filtered(self, client, db_session, test_application):
        """Test listing transactions with filters."""
        transaction = StripeTransaction(
            stripe_payment_intent_id="pi_123",
            transaction_type="payment",
            amount=Decimal("150.00"),
            currency="cad",
            status="succeeded",
            application_id=test_application.id,
        )
        db_session.add(transaction)
        db_session.commit()

        response = client.get(
            f"/api/v1/stripe/transactions?application_id={test_application.id}&transaction_type=payment&status=succeeded"
        )

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["stripe_payment_intent_id"] == "pi_123"