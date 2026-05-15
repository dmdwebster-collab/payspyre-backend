import logging
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status, Request, BackgroundTasks
from fastapi.security import HTTPBearer
from sqlalchemy.orm import Session

from app.db.base import get_db
from app.models.loan import Borrower, Vendor
from app.models.stripe import (
    PaymentMethod,
    StripeAccount,
    StripeTransaction,
    StripeWebhookEvent,
    StripePayout,
)
from app.models.funding import Payment, Refund
from app.schemas.stripe import (
    PaymentMethodCreate,
    PaymentMethodResponse,
    StripeAccountCreate,
    StripeAccountOnboardingRequest,
    StripeAccountOnboardingResponse,
    StripeAccountResponse,
    StripeAccountDetailResponse,
    StripeTransactionCreate,
    StripeTransactionResponse,
    StripeWebhookEventResponse,
    StripePayoutCreate,
    StripePayoutResponse,
    CustomerCreate,
    CustomerResponse,
    PaymentIntentCreate,
    PaymentIntentResponse,
    PaymentIntentConfirmRequest,
    DisbursementCreate,
    DisbursementResponse,
    PaymentMethodRegistrationRequest,
    PaymentMethodRegistrationResponse,
    SetupIntentCreate,
    SetupIntentResponse,
    RefundCreate,
    RefundResponse,
    BalanceResponse,
    VendorAccountBalanceResponse,
)
from app.services.stripe import stripe_service

router = APIRouter()
security = HTTPBearer()
logger = logging.getLogger(__name__)


@router.post("/customers", response_model=CustomerResponse, status_code=status.HTTP_201_CREATED)
async def create_stripe_customer(data: CustomerCreate, db: Session = Depends(get_db)):
    borrower = db.query(Borrower).filter(Borrower.id == data.borrower_id).first()
    if not borrower:
        raise HTTPException(status_code=404, detail="Borrower not found")

    result = await stripe_service.create_customer(
        patient_id=str(data.borrower_id),
        email=data.email,
        name=f"{borrower.first_name} {borrower.last_name}",
        phone=data.phone or borrower.phone,
    )

    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])

    return CustomerResponse(
        customer_id=result["customer_id"],
        borrower_id=data.borrower_id,
    )


@router.post("/payment-methods/register", response_model=PaymentMethodRegistrationResponse)
async def register_payment_method(
    borrower_id: UUID,
    data: PaymentMethodRegistrationRequest,
    db: Session = Depends(get_db),
):
    borrower = db.query(Borrower).filter(Borrower.id == borrower_id).first()
    if not borrower:
        raise HTTPException(status_code=404, detail="Borrower not found")

    # Get or create Stripe customer
    existing_payment_method = db.query(PaymentMethod).filter(
        PaymentMethod.borrower_id == borrower_id,
        PaymentMethod.stripe_customer_id.isnot(None),
    ).first()

    if not existing_payment_method:
        customer_result = await stripe_service.create_customer(
            patient_id=str(borrower_id),
            email=borrower.email,
            name=f"{borrower.first_name} {borrower.last_name}",
            phone=borrower.phone,
        )
        if not customer_result["success"]:
            return PaymentMethodRegistrationResponse(
                success=False,
                error=customer_result["error"],
            )
        customer_id = customer_result["customer_id"]
    else:
        customer_id = existing_payment_method.stripe_customer_id

    # Create payment method from token
    if not data.token:
        return PaymentMethodRegistrationResponse(
            success=False,
            error="Token is required for payment method registration",
        )

    pm_result = await stripe_service.create_payment_method(
        patient_id=str(borrower_id),
        payment_method_type=data.payment_method_type,
        card_token=data.token if data.payment_method_type == "card" else None,
        bank_account_token=data.token if data.payment_method_type == "us_bank_account" else None,
    )

    if not pm_result["success"]:
        return PaymentMethodRegistrationResponse(
            success=False,
            error=pm_result["error"],
        )

    # Attach to customer
    attach_result = await stripe_service.attach_payment_method_to_customer(
        customer_id=customer_id,
        payment_method_id=pm_result["payment_method_id"],
        is_default=data.is_default,
    )

    if not attach_result["success"]:
        return PaymentMethodRegistrationResponse(
            success=False,
            error=attach_result["error"],
        )

    # Save to database
    stripe_pm = attach_result["payment_method"]
    payment_method = PaymentMethod(
        borrower_id=borrower_id,
        stripe_payment_method_id=stripe_pm.id,
        stripe_customer_id=customer_id,
        payment_method_type=data.payment_method_type,
        card_last_4=stripe_pm.card.get("last4") if stripe_pm.type == "card" else None,
        card_brand=stripe_pm.card.get("brand") if stripe_pm.type == "card" else None,
        card_exp_month=stripe_pm.card.get("exp_month") if stripe_pm.type == "card" else None,
        card_exp_year=stripe_pm.card.get("exp_year") if stripe_pm.type == "card" else None,
        bank_account_last_4=stripe_pm.us_bank_account.get("last4") if stripe_pm.type == "us_bank_account" else None,
        bank_account_bank_name=stripe_pm.us_bank_account.get("bank_name") if stripe_pm.type == "us_bank_account" else None,
        is_default=data.is_default,
        is_verified=stripe_pm.type == "card" and stripe_pm.card.get("checks", {}).get("cvc_check") == "pass",
        stripe_response=stripe_pm.to_dict(),
    )

    if data.is_default:
        db.query(PaymentMethod).filter(
            PaymentMethod.borrower_id == borrower_id,
            PaymentMethod.id != payment_method.id,
        ).update({"is_default": False})

    db.add(payment_method)
    db.commit()

    return PaymentMethodRegistrationResponse(
        success=True,
        payment_method_id=str(payment_method.id),
        customer_id=customer_id,
    )


@router.get("/borrowers/{borrower_id}/payment-methods", response_model=List[PaymentMethodResponse])
async def list_payment_methods(
    borrower_id: UUID,
    db: Session = Depends(get_db),
):
    payment_methods = db.query(PaymentMethod).filter(
        PaymentMethod.borrower_id == borrower_id,
        PaymentMethod.status == "active",
    ).order_by(PaymentMethod.is_default.desc(), PaymentMethod.created_at.desc()).all()
    return payment_methods


@router.post("/payment-intents", response_model=PaymentIntentResponse)
async def create_payment_intent(
    data: PaymentIntentCreate,
    db: Session = Depends(get_db),
):
    result = await stripe_service.create_payment_intent(
        amount=data.amount,
        currency=data.currency,
        customer_id=data.customer_id,
        payment_method_id=data.payment_method_id,
        application_id=str(data.application_id) if data.application_id else None,
        metadata=data.metadata,
        confirm=data.confirm,
        off_session=data.off_session,
    )

    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])

    return PaymentIntentResponse(
        payment_intent_id=result["payment_intent_id"],
        client_secret=result.get("client_secret"),
        amount=data.amount,
        currency=data.currency,
        status=result["payment_intent"].status,
        application_id=data.application_id,
    )


@router.post("/payment-intents/{payment_intent_id}/confirm", response_model=PaymentIntentResponse)
async def confirm_payment_intent(
    payment_intent_id: str,
    data: PaymentIntentConfirmRequest,
    db: Session = Depends(get_db),
):
    result = await stripe_service.confirm_payment_intent(
        payment_intent_id=payment_intent_id,
        payment_method_id=data.payment_method_id,
    )

    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])

    pi = result["payment_intent"]

    # Log transaction
    transaction = StripeTransaction(
        stripe_payment_intent_id=payment_intent_id,
        transaction_type="payment",
        amount=Decimal(str(pi.amount / 100)),
        currency=pi.currency,
        status="processing" if pi.status == "processing" else "succeeded" if pi.status == "succeeded" else pi.status,
        stripe_fee=Decimal(str(pi.amount / 100 * 0.029 + 0.30)) if pi.amount else None,
        stripe_response=pi.to_dict(),
    )

    if pi.application:
        from uuid import UUID
        if "application_id" in pi.metadata:
            try:
                transaction.application_id = UUID(pi.metadata["application_id"])
            except (ValueError, TypeError):
                pass

    db.add(transaction)
    db.commit()

    return PaymentIntentResponse(
        payment_intent_id=payment_intent_id,
        client_secret=None,
        amount=Decimal(str(pi.amount / 100)),
        currency=pi.currency,
        status=pi.status,
        application_id=transaction.application_id,
    )


@router.post("/setup-intents", response_model=SetupIntentResponse)
async def create_setup_intent(data: SetupIntentCreate):
    result = await stripe_service.create_setup_intent(
        customer_id=data.customer_id,
        payment_method_types=data.payment_method_types,
    )

    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])

    return SetupIntentResponse(
        setup_intent_id=result["setup_intent_id"],
        client_secret=result["client_secret"],
    )


@router.post("/vendors/{vendor_id}/stripe-accounts", response_model=StripeAccountResponse, status_code=status.HTTP_201_CREATED)
async def create_stripe_account(
    vendor_id: UUID,
    data: StripeAccountCreate,
    db: Session = Depends(get_db),
):
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    existing_account = db.query(StripeAccount).filter(StripeAccount.vendor_id == vendor_id).first()
    if existing_account:
        raise HTTPException(status_code=400, detail="Vendor already has a Stripe account")

    result = await stripe_service.create_connect_account(
        vendor_id=str(vendor_id),
        email=vendor.email,
        business_name=vendor.business_name,
        business_type="company",
    )

    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])

    stripe_account = StripeAccount(
        vendor_id=vendor_id,
        stripe_account_id=result["account_id"],
        stripe_account_type=data.stripe_account_type,
        default_payout_schedule=data.default_payout_schedule,
        stripe_account_data=result["account"].to_dict(),
    )

    db.add(stripe_account)
    db.commit()
    db.refresh(stripe_account)

    return stripe_account


@router.post("/stripe-accounts/{stripe_account_id}/onboarding", response_model=StripeAccountOnboardingResponse)
async def generate_onboarding_link(
    stripe_account_id: str,
    data: StripeAccountOnboardingRequest,
    db: Session = Depends(get_db),
):
    account = db.query(StripeAccount).filter(StripeAccount.stripe_account_id == stripe_account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Stripe account not found")

    result = await stripe_service.generate_onboarding_link(
        account_id=stripe_account_id,
        refresh_url=data.refresh_url,
        return_url=data.return_url,
    )

    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])

    # Update account with onboarding link
    account.onboarding_url = result["url"]
    from datetime import datetime
    account.onboarding_url_expires_at = datetime.fromtimestamp(result["expires_at"])
    db.commit()

    return StripeAccountOnboardingResponse(
        url=result["url"],
        expires_at=result["expires_at"],
    )


@router.get("/stripe-accounts/{stripe_account_id}", response_model=StripeAccountDetailResponse)
async def get_stripe_account(stripe_account_id: str, db: Session = Depends(get_db)):
    account = db.query(StripeAccount).filter(StripeAccount.stripe_account_id == stripe_account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Stripe account not found")

    result = await stripe_service.retrieve_account(stripe_account_id)
    if result["success"]:
        account.stripe_account_data = result["account"].to_dict()
        account.charges_enabled = result["account"].get("charges_enabled", False)
        account.payouts_enabled = result["account"].get("payouts_enabled", False)
        account.details_submitted = result["account"].get("details_submitted", False)
        if account.details_submitted and not account.onboarding_completed_at:
            from datetime import datetime
            account.onboarding_status = "completed"
            account.onboarding_completed_at = datetime.utcnow()
        db.commit()

    return account


@router.get("/vendors/{vendor_id}/stripe-account", response_model=StripeAccountDetailResponse)
async def get_vendor_stripe_account(vendor_id: UUID, db: Session = Depends(get_db)):
    account = db.query(StripeAccount).filter(StripeAccount.vendor_id == vendor_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Vendor has no Stripe account")

    result = await stripe_service.retrieve_account(account.stripe_account_id)
    if result["success"]:
        account.stripe_account_data = result["account"].to_dict()
        account.charges_enabled = result["account"].get("charges_enabled", False)
        account.payouts_enabled = result["account"].get("payouts_enabled", False)
        account.details_submitted = result["account"].get("details_submitted", False)
        if account.details_submitted and not account.onboarding_completed_at:
            from datetime import datetime
            account.onboarding_status = "completed"
            account.onboarding_completed_at = datetime.utcnow()
        db.commit()

    return account


@router.post("/disbursements", response_model=DisbursementResponse)
async def create_disbursement(data: DisbursementCreate, db: Session = Depends(get_db)):
    account = db.query(StripeAccount).filter(
        StripeAccount.stripe_account_id == data.vendor_stripe_account_id
    ).first()

    if not account:
        raise HTTPException(status_code=404, detail="Stripe account not found")

    if not account.charges_enabled or not account.payouts_enabled:
        raise HTTPException(
            status_code=400,
            detail="Stripe account must be fully verified before disbursement",
        )

    result = await stripe_service.create_disbursement(
        amount=data.amount,
        vendor_stripe_account_id=data.vendor_stripe_account_id,
        application_id=str(data.application_id) if data.application_id else None,
        reference_number=data.reference_number,
    )

    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])

    # Log transaction
    transaction = StripeTransaction(
        stripe_account_id=account.id,
        stripe_transfer_id=result["transfer_id"],
        transaction_type="disbursement",
        amount=data.amount,
        currency="cad",
        status="pending",
        transfer_group=result["transfer_group"],
        application_id=data.application_id,
    )

    if data.application_id:
        from app.models.funding import Funding
        funding = db.query(Funding).filter(Funding.application_id == data.application_id).first()
        if funding:
            funding.stripe_transfer_id = result["transfer_id"]
            funding.status = "processing"

    db.add(transaction)
    db.commit()

    return DisbursementResponse(
        success=True,
        transfer_id=result["transfer_id"],
        transfer_group=result["transfer_group"],
        error=None,
    )


@router.post("/payouts", response_model=StripePayoutResponse, status_code=status.HTTP_201_CREATED)
async def create_payout(data: StripePayoutCreate, db: Session = Depends(get_db)):
    account = db.query(StripeAccount).filter(StripeAccount.id == data.stripe_account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Stripe account not found")

    if not account.payouts_enabled:
        raise HTTPException(
            status_code=400,
            detail="Stripe account must have payouts enabled",
        )

    result = await stripe_service.create_payout(
        stripe_account_id=account.stripe_account_id,
        amount=data.amount,
        currency=data.currency,
    )

    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])

    stripe_payout = result["payout"]
    payout = StripePayout(
        stripe_account_id=account.id,
        stripe_payout_id=stripe_payout.id,
        amount=Decimal(str(stripe_payout.amount / 100)),
        currency=stripe_payout.currency,
        status=stripe_payout.status,
        stripe_response=stripe_payout.to_dict(),
    )

    db.add(payout)
    db.commit()
    db.refresh(payout)

    return payout


@router.get("/stripe-accounts/{stripe_account_id}/payouts", response_model=List[StripePayoutResponse])
async def list_payouts(stripe_account_id: str, db: Session = Depends(get_db)):
    account = db.query(StripeAccount).filter(StripeAccount.stripe_account_id == stripe_account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Stripe account not found")

    payouts = db.query(StripePayout).filter(
        StripePayout.stripe_account_id == account.id
    ).order_by(StripePayout.created_at.desc()).all()

    return payouts


@router.post("/refunds", response_model=RefundResponse)
async def create_refund(data: RefundCreate, db: Session = Depends(get_db)):
    result = await stripe_service.create_refund(
        payment_intent_id=data.payment_intent_id,
        amount=data.amount,
        reason=data.reason,
        metadata=data.metadata,
    )

    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])

    # Update refund record if exists
    existing_refund = db.query(Refund).join(Payment).filter(
        Payment.transaction_id == data.payment_intent_id
    ).first()

    if existing_refund:
        existing_refund.status = "completed"
        existing_refund.reference_number = result["refund_id"]
        existing_refund.processed_at = datetime.utcnow()
        db.commit()

    return RefundResponse(
        success=True,
        refund_id=result["refund_id"],
        error=None,
    )


@router.get("/balance", response_model=BalanceResponse)
async def get_platform_balance():
    result = await stripe_service.retrieve_balance()
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])

    balance = result["balance"]
    return BalanceResponse(
        available=[{
            "amount": Decimal(str(item.amount / 100)),
            "currency": item.currency,
        } for item in balance.get("available", [])],
        pending=[{
            "amount": Decimal(str(item.amount / 100)),
            "currency": item.currency,
        } for item in balance.get("pending", [])],
    )


@router.get("/vendors/{vendor_id}/balance", response_model=VendorAccountBalanceResponse)
async def get_vendor_balance(vendor_id: UUID, db: Session = Depends(get_db)):
    account = db.query(StripeAccount).filter(StripeAccount.vendor_id == vendor_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="Vendor has no Stripe account")

    result = await stripe_service.retrieve_account_balance(account.stripe_account_id)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])

    balance = result["balance"]
    return VendorAccountBalanceResponse(
        available=[{
            "amount": Decimal(str(item.amount / 100)),
            "currency": item.currency,
        } for item in balance.get("available", [])],
        pending=[{
            "amount": Decimal(str(item.amount / 100)),
            "currency": item.currency,
        } for item in balance.get("pending", [])],
        vendor_id=vendor_id,
    )


@router.post("/webhook", status_code=status.HTTP_200_OK)
async def stripe_webhook(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    if not sig_header:
        raise HTTPException(status_code=400, detail="Missing stripe-signature header")

    result = stripe_service.construct_webhook_event(payload, sig_header)
    if not result["success"]:
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    event = result["event"]

    # Check for duplicate events
    existing_event = db.query(StripeWebhookEvent).filter(
        StripeWebhookEvent.stripe_event_id == event.id
    ).first()

    if existing_event:
        logger.info(f"Duplicate webhook event: {event.id}")
        return {"status": "duplicate"}

    # Store event
    webhook_event = StripeWebhookEvent(
        stripe_event_id=event.id,
        event_type=event.type,
        api_version=event.api_version,
        event_data=event.data,
    )
    db.add(webhook_event)

    background_tasks.add_task(process_webhook_event, event, webhook_event, db)

    db.commit()

    return {"status": "received"}


async def process_webhook_event(event, webhook_event, db: Session):
    """Process Stripe webhook events asynchronously."""
    try:
        event_type = event.type
        data = event.data.object

        if event_type == "payment_intent.succeeded":
            payment_intent_id = data.id
            application_id = None

            if "application_id" in data.metadata:
                try:
                    from uuid import UUID
                    application_id = UUID(data.metadata["application_id"])
                except (ValueError, TypeError):
                    pass

            # Update or create transaction record
            transaction = db.query(StripeTransaction).filter(
                StripeTransaction.stripe_payment_intent_id == payment_intent_id
            ).first()

            if transaction:
                transaction.status = "succeeded"
                transaction.processed_at = datetime.utcnow()
                transaction.stripe_response = data.to_dict()
            else:
                transaction = StripeTransaction(
                    stripe_payment_intent_id=payment_intent_id,
                    transaction_type="payment",
                    amount=Decimal(str(data.amount / 100)),
                    currency=data.currency,
                    status="succeeded",
                    stripe_fee=Decimal(str(data.amount / 100 * 0.029 + 0.30)) if data.amount else None,
                    stripe_response=data.to_dict(),
                    application_id=application_id,
                    processed_at=datetime.utcnow(),
                )
                db.add(transaction)

            # Update payment record if linked
            if application_id:
                from app.models.funding import Payment
                payment = db.query(Payment).filter(
                    Payment.application_id == application_id,
                    Payment.transaction_id.is_(None),
                ).first()
                if payment:
                    payment.transaction_id = payment_intent_id
                    payment.status = "completed"

            webhook_event.related_transaction_id = transaction.id

        elif event_type == "payment_intent.payment_failed":
            payment_intent_id = data.id

            transaction = db.query(StripeTransaction).filter(
                StripeTransaction.stripe_payment_intent_id == payment_intent_id
            ).first()

            if transaction:
                transaction.status = "failed"
                transaction.failure_code = data.last_payment_error.get("code") if data.last_payment_error else None
                transaction.failure_message = data.last_payment_error.get("message") if data.last_payment_error else None
                transaction.processed_at = datetime.utcnow()
                transaction.stripe_response = data.to_dict()

                webhook_event.related_transaction_id = transaction.id

        elif event_type == "transfer.paid":
            transfer_id = data.id

            transaction = db.query(StripeTransaction).filter(
                StripeTransaction.stripe_transfer_id == transfer_id
            ).first()

            if transaction:
                transaction.status = "succeeded"
                transaction.processed_at = datetime.utcnow()
                transaction.stripe_response = data.to_dict()

                # Update funding status
                if transaction.application_id:
                    from app.models.funding import Funding
                    funding = db.query(Funding).filter(Funding.application_id == transaction.application_id).first()
                    if funding:
                        funding.status = "completed"

                webhook_event.related_transaction_id = transaction.id

        elif event_type == "transfer.failed":
            transfer_id = data.id

            transaction = db.query(StripeTransaction).filter(
                StripeTransaction.stripe_transfer_id == transfer_id
            ).first()

            if transaction:
                transaction.status = "failed"
                transaction.failure_code = data.get("failure_code")
                transaction.failure_message = data.get("failure_message")
                transaction.processed_at = datetime.utcnow()
                transaction.stripe_response = data.to_dict()

                # Update funding status
                if transaction.application_id:
                    from app.models.funding import Funding
                    funding = db.query(Funding).filter(Funding.application_id == transaction.application_id).first()
                    if funding:
                        funding.status = "failed"
                        funding.failure_reason = data.get("failure_message", "Stripe transfer failed")

                webhook_event.related_transaction_id = transaction.id

        elif event_type == "payout.created":
            payout_id = data.id
            destination_id = data.destination

            account = db.query(StripeAccount).filter(
                StripeAccount.stripe_account_id == destination_id
            ).first()

            if account:
                payout = StripePayout(
                    stripe_account_id=account.id,
                    stripe_payout_id=payout_id,
                    amount=Decimal(str(data.amount / 100)),
                    currency=data.currency,
                    status=data.status,
                    arrival_date=datetime.fromtimestamp(data.arrival_date) if data.arrival_date else None,
                    stripe_response=data.to_dict(),
                )
                db.add(payout)

        elif event_type == "payout.paid":
            payout_id = data.id

            payout = db.query(StripePayout).filter(
                StripePayout.stripe_payout_id == payout_id
            ).first()

            if payout:
                payout.status = "paid"
                payout.stripe_response = data.to_dict()

        elif event_type == "payout.failed":
            payout_id = data.id

            payout = db.query(StripePayout).filter(
                StripePayout.stripe_payout_id == payout_id
            ).first()

            if payout:
                payout.status = "failed"
                payout.failure_code = data.get("failure_code")
                payout.failure_message = data.get("failure_message")
                payout.stripe_response = data.to_dict()

        elif event_type == "charge.refunded":
            refund_id = data.id
            payment_intent_id = data.payment_intent

            transaction = StripeTransaction(
                stripe_refund_id=refund_id,
                stripe_payment_intent_id=payment_intent_id,
                transaction_type="refund",
                amount=Decimal(str(data.amount_refunded / 100)),
                currency=data.currency,
                status="succeeded",
                stripe_response=data.to_dict(),
            )
            db.add(transaction)
            webhook_event.related_transaction_id = transaction.id

        elif event_type == "account.updated":
            account_id = data.id

            account = db.query(StripeAccount).filter(
                StripeAccount.stripe_account_id == account_id
            ).first()

            if account:
                account.stripe_account_data = data.to_dict()
                account.charges_enabled = data.get("charges_enabled", False)
                account.payouts_enabled = data.get("payouts_enabled", False)
                account.details_submitted = data.get("details_submitted", False)

                if account.details_submitted and not account.onboarding_completed_at:
                    account.onboarding_status = "completed"
                    account.onboarding_completed_at = datetime.utcnow()

        webhook_event.processed = True
        webhook_event.processed_at = datetime.utcnow()
        db.commit()

        logger.info(f"Processed webhook event: {event_type}")

    except Exception as e:
        logger.error(f"Error processing webhook event {event.id}: {e}")
        webhook_event.processing_error = str(e)
        webhook_event.processed = True
        webhook_event.processed_at = datetime.utcnow()
        db.commit()


@router.get("/transactions", response_model=List[StripeTransactionResponse])
async def list_transactions(
    application_id: UUID = None,
    transaction_type: str = None,
    status: str = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    query = db.query(StripeTransaction)

    if application_id:
        query = query.filter(StripeTransaction.application_id == application_id)
    if transaction_type:
        query = query.filter(StripeTransaction.transaction_type == transaction_type)
    if status:
        query = query.filter(StripeTransaction.status == status)

    transactions = query.order_by(StripeTransaction.created_at.desc()).offset(skip).limit(limit).all()
    return transactions


@router.get("/webhook-events", response_model=List[StripeWebhookEventResponse])
async def list_webhook_events(
    processed: bool = None,
    event_type: str = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    query = db.query(StripeWebhookEvent)

    if processed is not None:
        query = query.filter(StripeWebhookEvent.processed == processed)
    if event_type:
        query = query.filter(StripeWebhookEvent.event_type == event_type)

    events = query.order_by(StripeWebhookEvent.created_at.desc()).offset(skip).limit(limit).all()
    return events