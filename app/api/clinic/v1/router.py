"""Clinic API v1 router assembly. Mounted at /api/clinic/v1 in app/main.py.

Endpoints (all staff-authenticated via the platform JWT):
    GET  /clinic/v1/products
    GET  /clinic/v1/applications
    GET  /clinic/v1/dashboard/summary
    POST /clinic/v1/financing-links
"""
from fastapi import APIRouter

from app.api.clinic.v1.endpoints import applications, financing_links, marketplace, products

clinic_router = APIRouter()
clinic_router.include_router(products.router)
clinic_router.include_router(applications.router)
clinic_router.include_router(financing_links.router)
clinic_router.include_router(marketplace.router)
