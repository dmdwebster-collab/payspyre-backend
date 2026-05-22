from fastapi import APIRouter

from app.api.v1.endpoints import auth, credit, documents, funding, kyc, loan, notifications, patients, underwriting, vendor, stripe, analytics, health

api_router = APIRouter()
api_router.include_router(health.router, tags=["health"])
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(kyc.router, tags=["kyc"])
api_router.include_router(loan.router, prefix="/loan", tags=["loan"])
api_router.include_router(credit.router, tags=["credit"])
api_router.include_router(underwriting.router, prefix="/underwriting", tags=["underwriting"])
api_router.include_router(funding.router, prefix="/funding", tags=["funding"])
api_router.include_router(vendor.router, prefix="/vendors", tags=["vendors"])
api_router.include_router(patients.router, prefix="/patients", tags=["patients"])
api_router.include_router(documents.router, prefix="/documents", tags=["documents"])
api_router.include_router(stripe.router, prefix="/stripe", tags=["stripe"])
api_router.include_router(notifications.router, prefix="/notifications", tags=["notifications"])
api_router.include_router(analytics.router, prefix="/analytics", tags=["analytics"])