"""Vendor webhook v1 router assembly. Mounted at /api/webhooks/v1 in app/main.py."""
from fastapi import APIRouter

from app.api.webhooks.v1.endpoints import verification

webhook_router = APIRouter()
webhook_router.include_router(verification.router)
