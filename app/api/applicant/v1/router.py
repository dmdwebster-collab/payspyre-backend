"""Applicant API v1 router assembly. Mounted at /api/applicant/v1 in app/main.py."""
from fastapi import APIRouter

from app.api.applicant.v1.endpoints import applications, auth

applicant_router = APIRouter()
applicant_router.include_router(auth.router)
applicant_router.include_router(applications.router)
