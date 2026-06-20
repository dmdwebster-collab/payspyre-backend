"""Vendor webhook v1 router assembly. Mounted at /api/webhooks/v1 in app/main.py."""
from fastapi import APIRouter

from app.api.webhooks.v1.endpoints import esign, notifications, payments, verification

webhook_router = APIRouter()
webhook_router.include_router(verification.router)
webhook_router.include_router(notifications.router)  # P7.4b — Twilio + Resend
webhook_router.include_router(esign.router)  # P8.x — SignNow agreement lifecycle
webhook_router.include_router(payments.router)  # P8.x — Zumrails disbursement lifecycle
