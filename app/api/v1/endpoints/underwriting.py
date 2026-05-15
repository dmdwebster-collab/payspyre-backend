from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.db.base import get_db
from app.models.loan import LoanApplication
from app.models.kyc import KycResult, KycSession
from app.schemas.underwriting import (
    UnderwritingDecisionResponse,
    UnderwritingManualReviewRequest,
    UnderwritingStatusResponse,
)
from app.services.risk_engine import RiskRulesEngine

router = APIRouter()
risk_engine = RiskRulesEngine()
limiter = Limiter(key_func=get_remote_address)


@router.post("/evaluate", response_model=UnderwritingDecisionResponse)
@limiter.limit("20/minute")
async def evaluate_application(
    request: Request,
    application_id: UUID,
    db: Session = Depends(get_db),
):
    """Run full underwriting evaluation on an application"""
    application = db.query(LoanApplication).filter(LoanApplication.id == application_id).first()
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")

    if application.status not in ("pending_documents", "underwriting"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot evaluate application in '{application.status}' status",
        )

    # Get KYC results for the borrower
    kyc_sessions = db.query(KycSession).filter(
        KycSession.borrower_id == application.borrower_id,
        KycSession.status == "completed"
    ).all()

    if not kyc_sessions:
        raise HTTPException(
            status_code=400,
            detail="No completed KYC sessions found. Application must complete KYC first.",
        )

    session_ids = [s.id for s in kyc_sessions]
    kyc_results = db.query(KycResult).filter(
        KycResult.kyc_session_id.in_(session_ids)
    ).all()

    if not kyc_results:
        raise HTTPException(
            status_code=400,
            detail="No KYC results found. Application must complete KYC first.",
        )

    # Build KYC data structure for risk engine
    kyc_data = {
        "checks": [
            {
                "type": result.check_type,
                "status": result.check_status,
                "details": result.check_details or {},
                "score": float(result.score) if result.score else None,
                "flags": result.flags or [],
            }
            for result in kyc_results
        ],
    }

    # Build loan application data for risk engine
    borrower = application.borrower
    loan_app_data = {
        "loan_amount": float(application.requested_amount),
        "address": {
            "country": borrower.country,
            "province": borrower.province,
            "city": borrower.city,
        },
        "credit_history_months": 0,
        "employment_income": float(borrower.employment_income) if borrower.employment_income else 0,
    }

    # Run risk evaluation
    evaluation = await risk_engine.evaluate(kyc_data, loan_app_data)

    # Update application with decision
    application.decision = evaluation.decision
    application.decision_reason = evaluation.reason
    application.decision_at = datetime.utcnow()

    if evaluation.decision == "approve":
        application.status = "approved"
        application.approved_at = datetime.utcnow()
    elif evaluation.decision == "reject":
        application.status = "rejected"
    else:
        application.status = "underwriting"

    db.commit()
    db.refresh(application)

    return UnderwritingDecisionResponse(
        application_id=application.id,
        decision=evaluation.decision,
        reason=evaluation.reason,
        risk_score=evaluation.risk_score,
        flags_applied=evaluation.flags_applied,
        status=application.status,
        decision_at=application.decision_at,
    )


@router.post("/manual-review", response_model=UnderwritingDecisionResponse)
@limiter.limit("10/minute")
async def submit_manual_review(
    request: Request,
    data: UnderwritingManualReviewRequest,
    db: Session = Depends(get_db),
):
    """Submit a manual underwriting review decision"""
    application = db.query(LoanApplication).filter(LoanApplication.id == data.application_id).first()
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")

    if application.status != "underwriting":
        raise HTTPException(
            status_code=400,
            detail=f"Only applications in 'underwriting' status can be manually reviewed",
        )

    if data.approved:
        application.decision = "approved"
        application.status = "approved"
        application.decision_reason = data.notes or "Manually approved"
        application.approved_at = datetime.utcnow()
    else:
        application.decision = "rejected"
        application.status = "rejected"
        application.decision_reason = data.notes or "Manually rejected"

    application.decision_at = datetime.utcnow()

    db.commit()
    db.refresh(application)

    return UnderwritingDecisionResponse(
        application_id=application.id,
        decision=application.decision,
        reason=application.decision_reason,
        risk_score=0.0,
        flags_applied=[],
        status=application.status,
        decision_at=application.decision_at,
    )


@router.get("/status/{application_id}", response_model=UnderwritingStatusResponse)
@limiter.limit("100/minute")
async def get_underwriting_status(request: Request, application_id: UUID, db: Session = Depends(get_db)):
    """Get current underwriting status for an application"""
    application = db.query(LoanApplication).filter(LoanApplication.id == application_id).first()
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")

    return UnderwritingStatusResponse(
        application_id=application.id,
        status=application.status,
        decision=application.decision,
        decision_reason=application.decision_reason,
        decision_at=application.decision_at,
        created_at=application.created_at,
        updated_at=application.updated_at,
    )


@router.post("/request-rereview/{application_id}", response_model=UnderwritingStatusResponse)
@limiter.limit("5/minute")
async def request_rereview(request: Request, application_id: UUID, db: Session = Depends(get_db)):
    """Request a manual review for an automated decision"""
    application = db.query(LoanApplication).filter(LoanApplication.id == application_id).first()
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")

    if application.status not in ("approved", "rejected"):
        raise HTTPException(
            status_code=400,
            detail="Only approved or rejected applications can be requested for re-review",
        )

    application.status = "underwriting"
    application.decision_reason = f"Re-review requested. Previous: {application.decision_reason}"

    db.commit()
    db.refresh(application)

    return UnderwritingStatusResponse(
        application_id=application.id,
        status=application.status,
        decision=application.decision,
        decision_reason=application.decision_reason,
        decision_at=application.decision_at,
        created_at=application.created_at,
        updated_at=application.updated_at,
    )