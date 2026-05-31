from fastapi import APIRouter

from app.api.v1.endpoints import auth, credit, credit_products, funding, loan, patients, underwriting, vendor, analytics, health

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(loan.router, prefix="/loan", tags=["loan"])
api_router.include_router(credit.router, tags=["credit"])
api_router.include_router(credit_products.router, prefix="/credit-products", tags=["credit-products"])
api_router.include_router(underwriting.router, prefix="/underwriting", tags=["underwriting"])
api_router.include_router(funding.router, prefix="/funding", tags=["funding"])
api_router.include_router(vendor.router, prefix="/vendors", tags=["vendors"])
api_router.include_router(patients.router, prefix="/patients", tags=["patients"])
api_router.include_router(analytics.router, prefix="/analytics", tags=["analytics"])
# V1 notifications router removed in P7.4c (un-mounted; files deleted in commit B).