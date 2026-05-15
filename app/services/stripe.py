import os
import logging
from typing import Optional, Dict, Any
from decimal import Decimal

import stripe
from datetime import datetime, date

from app.core.config import settings

logger = logging.getLogger(__name__)

stripe.api_key = getattr(settings, "STRIPE_SECRET_KEY", os.getenv("STRIPE_SECRET_KEY"))

STRIPE_WEBHOOK_SECRET = getattr(settings, "STRIPE_WEBHOOK_SECRET", os.getenv("STRIPE_WEBHOOK_SECRET"))


class StripeService:
    """Stripe integration service for payments and disbursements."""

    @staticmethod
    async def create_connect_account(
        vendor_id: str,
        email: str,
        business_name: str,
        business_type: str,
        country: str = "CA",
    ) -> Dict[str, Any]:
        """Create a Stripe Connect Express account for a vendor."""
        try:
            account = stripe.Account.create(
                type="express",
                country=country,
                email=email,
                business_type=business_type,
                business_profile={
                    "name": business_name,
                    "mcc": "8021",  # Medical and Dental Services
                },
                capabilities={
                    "transfers": {"requested": True},
                    "card_payments": {"requested": True},
                },
                metadata={
                    "vendor_id": str(vendor_id),
                    "integration": "payspyre",
                },
            )

            return {
                "success": True,
                "account_id": account.id,
                "account": account,
            }
        except stripe.error.StripeError as e:
            logger.error(f"Stripe Connect account creation failed: {e}")
            return {
                "success": False,
                "error": str(e),
            }

    @staticmethod
    async def generate_onboarding_link(account_id: str, refresh_url: str, return_url: str) -> Dict[str, Any]:
        """Generate Stripe Connect onboarding link for vendor."""
        try:
            account_link = stripe.AccountLink.create(
                account=account_id,
                refresh_url=refresh_url,
                return_url=return_url,
                type="account_onboarding",
            )

            return {
                "success": True,
                "url": account_link.url,
                "expires_at": account_link.expires_at,
            }
        except stripe.error.StripeError as e:
            logger.error(f"Stripe onboarding link generation failed: {e}")
            return {
                "success": False,
                "error": str(e),
            }

    @staticmethod
    async def retrieve_account(account_id: str) -> Dict[str, Any]:
        """Retrieve Stripe Connect account details."""
        try:
            account = stripe.Account.retrieve(account_id)
            return {
                "success": True,
                "account": account,
            }
        except stripe.error.StripeError as e:
            logger.error(f"Stripe account retrieval failed: {e}")
            return {
                "success": False,
                "error": str(e),
            }

    @staticmethod
    async def create_payment_method(
        patient_id: str,
        payment_method_type: str = "card",
        card_token: Optional[str] = None,
        bank_account_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create and attach a payment method for a patient."""
        try:
            if payment_method_type == "card" and card_token:
                payment_method = stripe.PaymentMethod.create(
                    type="card",
                    card={"token": card_token},
                    metadata={
                        "patient_id": str(patient_id),
                        "integration": "payspyre",
                    },
                )
            elif payment_method_type == "us_bank_account" and bank_account_token:
                payment_method = stripe.PaymentMethod.create(
                    type="us_bank_account",
                    us_bank_account={"token": bank_account_token},
                    metadata={
                        "patient_id": str(patient_id),
                        "integration": "payspyre",
                    },
                )
            else:
                return {
                    "success": False,
                    "error": "Invalid payment method type or missing token",
                }

            return {
                "success": True,
                "payment_method_id": payment_method.id,
                "payment_method": payment_method,
            }
        except stripe.error.StripeError as e:
            logger.error(f"Stripe payment method creation failed: {e}")
            return {
                "success": False,
                "error": str(e),
            }

    @staticmethod
    async def attach_payment_method_to_customer(
        customer_id: str,
        payment_method_id: str,
        is_default: bool = False,
    ) -> Dict[str, Any]:
        """Attach a payment method to a customer."""
        try:
            payment_method = stripe.PaymentMethod.attach(
                payment_method_id,
                customer=customer_id,
            )

            if is_default:
                stripe.Customer.modify(
                    customer_id,
                    invoice_settings={"default_payment_method": payment_method_id},
                )

            return {
                "success": True,
                "payment_method": payment_method,
            }
        except stripe.error.StripeError as e:
            logger.error(f"Stripe payment method attachment failed: {e}")
            return {
                "success": False,
                "error": str(e),
            }

    @staticmethod
    async def create_customer(
        patient_id: str,
        email: str,
        name: str,
        phone: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a Stripe customer for a patient."""
        try:
            customer = stripe.Customer.create(
                email=email,
                name=name,
                phone=phone,
                metadata={
                    "patient_id": str(patient_id),
                    "integration": "payspyre",
                },
            )

            return {
                "success": True,
                "customer_id": customer.id,
                "customer": customer,
            }
        except stripe.error.StripeError as e:
            logger.error(f"Stripe customer creation failed: {e}")
            return {
                "success": False,
                "error": str(e),
            }

    @staticmethod
    async def create_payment_intent(
        amount: Decimal,
        currency: str = "cad",
        customer_id: Optional[str] = None,
        payment_method_id: Optional[str] = None,
        application_id: Optional[str] = None,
        metadata: Optional[Dict[str, str]] = None,
        confirm: bool = False,
        off_session: bool = False,
    ) -> Dict[str, Any]:
        """Create a payment intent for patient payment."""
        try:
            intent_params = {
                "amount": int(amount * 100),
                "currency": currency,
                "metadata": {
                    "integration": "payspyre",
                    **(metadata or {}),
                },
            }

            if customer_id:
                intent_params["customer"] = customer_id

            if payment_method_id:
                intent_params["payment_method"] = payment_method_id
                intent_params["payment_method_types"] = ["card"]

            if application_id:
                intent_params["metadata"]["application_id"] = str(application_id)

            if confirm:
                intent_params["confirm"] = True
                intent_params["off_session"] = off_session

            payment_intent = stripe.PaymentIntent.create(**intent_params)

            return {
                "success": True,
                "payment_intent_id": payment_intent.id,
                "client_secret": payment_intent.client_secret,
                "payment_intent": payment_intent,
            }
        except stripe.error.StripeError as e:
            logger.error(f"Stripe payment intent creation failed: {e}")
            return {
                "success": False,
                "error": str(e),
            }

    @staticmethod
    async def confirm_payment_intent(
        payment_intent_id: str,
        payment_method_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Confirm a payment intent."""
        try:
            params = {}
            if payment_method_id:
                params["payment_method"] = payment_method_id

            payment_intent = stripe.PaymentIntent.confirm(payment_intent_id, **params)

            return {
                "success": True,
                "payment_intent": payment_intent,
            }
        except stripe.error.StripeError as e:
            logger.error(f"Stripe payment intent confirmation failed: {e}")
            return {
                "success": False,
                "error": str(e),
            }

    @staticmethod
    async def retrieve_payment_intent(payment_intent_id: str) -> Dict[str, Any]:
        """Retrieve payment intent details."""
        try:
            payment_intent = stripe.PaymentIntent.retrieve(payment_intent_id)
            return {
                "success": True,
                "payment_intent": payment_intent,
            }
        except stripe.error.StripeError as e:
            logger.error(f"Stripe payment intent retrieval failed: {e}")
            return {
                "success": False,
                "error": str(e),
            }

    @staticmethod
    async def cancel_payment_intent(payment_intent_id: str) -> Dict[str, Any]:
        """Cancel a payment intent."""
        try:
            payment_intent = stripe.PaymentIntent.cancel(payment_intent_id)
            return {
                "success": True,
                "payment_intent": payment_intent,
            }
        except stripe.error.StripeError as e:
            logger.error(f"Stripe payment intent cancellation failed: {e}")
            return {
                "success": False,
                "error": str(e),
            }

    @staticmethod
    async def create_transfer(
        amount: Decimal,
        destination: str,
        currency: str = "cad",
        transfer_group: Optional[str] = None,
        metadata: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Transfer funds to a vendor's Stripe Connect account."""
        try:
            transfer = stripe.Transfer.create(
                amount=int(amount * 100),
                currency=currency,
                destination=destination,
                transfer_group=transfer_group,
                metadata={
                    "integration": "payspyre",
                    **(metadata or {}),
                },
            )

            return {
                "success": True,
                "transfer_id": transfer.id,
                "transfer": transfer,
            }
        except stripe.error.StripeError as e:
            logger.error(f"Stripe transfer creation failed: {e}")
            return {
                "success": False,
                "error": str(e),
            }

    @staticmethod
    async def create_payout(
        stripe_account_id: str,
        amount: Optional[Decimal] = None,
        currency: str = "cad",
    ) -> Dict[str, Any]:
        """Create a payout from vendor's Stripe Connect account to their bank."""
        try:
            payout_params = {
                "currency": currency,
            }

            if amount:
                payout_params["amount"] = int(amount * 100)
            else:
                payout_params["amount"] = "payout_all"

            payout = stripe.Payout.create(
                stripe_account=stripe_account_id,
                **payout_params,
            )

            return {
                "success": True,
                "payout_id": payout.id,
                "payout": payout,
            }
        except stripe.error.StripeError as e:
            logger.error(f"Stripe payout creation failed: {e}")
            return {
                "success": False,
                "error": str(e),
            }

    @staticmethod
    async def create_disbursement(
        amount: Decimal,
        vendor_stripe_account_id: str,
        application_id: Optional[str] = None,
        reference_number: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a complete disbursement flow to a vendor."""
        try:
            transfer_group = reference_number or f"FND-{application_id[:8].upper() if application_id else datetime.now().strftime('%Y%m%d%H%M%S')}"

            transfer_result = await StripeService.create_transfer(
                amount=amount,
                destination=vendor_stripe_account_id,
                transfer_group=transfer_group,
                metadata={
                    "application_id": str(application_id) if application_id else "",
                    "reference_number": reference_number or "",
                    "disbursement_type": "loan_funding",
                },
            )

            if not transfer_result["success"]:
                return transfer_result

            return {
                "success": True,
                "transfer_id": transfer_result["transfer_id"],
                "transfer_group": transfer_group,
                "disbursement": transfer_result["transfer"],
            }
        except Exception as e:
            logger.error(f"Stripe disbursement creation failed: {e}")
            return {
                "success": False,
                "error": str(e),
            }

    @staticmethod
    async def create_setup_intent(
        customer_id: str,
        payment_method_types: Optional[list] = None,
    ) -> Dict[str, Any]:
        """Create a setup intent for saving payment methods."""
        try:
            payment_method_types = payment_method_types or ["card", "us_bank_account"]

            setup_intent = stripe.SetupIntent.create(
                customer=customer_id,
                payment_method_types=payment_method_types,
                metadata={
                    "integration": "payspyre",
                },
            )

            return {
                "success": True,
                "setup_intent_id": setup_intent.id,
                "client_secret": setup_intent.client_secret,
                "setup_intent": setup_intent,
            }
        except stripe.error.StripeError as e:
            logger.error(f"Stripe setup intent creation failed: {e}")
            return {
                "success": False,
                "error": str(e),
            }

    @staticmethod
    async def list_payment_methods(
        customer_id: str,
        payment_method_type: str = "card",
    ) -> Dict[str, Any]:
        """List payment methods for a customer."""
        try:
            payment_methods = stripe.PaymentMethod.list(
                customer=customer_id,
                type=payment_method_type,
            )

            return {
                "success": True,
                "payment_methods": payment_methods.data,
            }
        except stripe.error.StripeError as e:
            logger.error(f"Stripe payment methods listing failed: {e}")
            return {
                "success": False,
                "error": str(e),
            }

    @staticmethod
    async def detach_payment_method(payment_method_id: str) -> Dict[str, Any]:
        """Detach a payment method from a customer."""
        try:
            payment_method = stripe.PaymentMethod.detach(payment_method_id)
            return {
                "success": True,
                "payment_method": payment_method,
            }
        except stripe.error.StripeError as e:
            logger.error(f"Stripe payment method detachment failed: {e}")
            return {
                "success": False,
                "error": str(e),
            }

    @staticmethod
    async def create_refund(
        payment_intent_id: str,
        amount: Optional[Decimal] = None,
        reason: str = "requested_by_customer",
        metadata: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Create a refund for a payment."""
        try:
            refund_params = {
                "payment_intent": payment_intent_id,
                "reason": reason,
                "metadata": {
                    "integration": "payspyre",
                    **(metadata or {}),
                },
            }

            if amount:
                refund_params["amount"] = int(amount * 100)

            refund = stripe.Refund.create(**refund_params)

            return {
                "success": True,
                "refund_id": refund.id,
                "refund": refund,
            }
        except stripe.error.StripeError as e:
            logger.error(f"Stripe refund creation failed: {e}")
            return {
                "success": False,
                "error": str(e),
            }

    @staticmethod
    async def retrieve_balance() -> Dict[str, Any]:
        """Retrieve platform balance."""
        try:
            balance = stripe.Balance.retrieve()
            return {
                "success": True,
                "balance": balance,
            }
        except stripe.error.StripeError as e:
            logger.error(f"Stripe balance retrieval failed: {e}")
            return {
                "success": False,
                "error": str(e),
            }

    @staticmethod
    async def retrieve_account_balance(stripe_account_id: str) -> Dict[str, Any]:
        """Retrieve vendor Stripe account balance."""
        try:
            balance = stripe.Balance.retrieve(stripe_account=stripe_account_id)
            return {
                "success": True,
                "balance": balance,
            }
        except stripe.error.StripeError as e:
            logger.error(f"Stripe account balance retrieval failed: {e}")
            return {
                "success": False,
                "error": str(e),
            }

    @staticmethod
    def construct_webhook_event(payload: bytes, sig_header: str) -> Dict[str, Any]:
        """Construct webhook event from payload and signature."""
        try:
            event = stripe.Webhook.construct_event(
                payload,
                sig_header,
                STRIPE_WEBHOOK_SECRET,
            )
            return {
                "success": True,
                "event": event,
            }
        except ValueError as e:
            logger.error(f"Invalid webhook payload: {e}")
            return {
                "success": False,
                "error": "Invalid payload",
            }
        except stripe.error.SignatureVerificationError as e:
            logger.error(f"Invalid webhook signature: {e}")
            return {
                "success": False,
                "error": "Invalid signature",
            }


stripe_service = StripeService()