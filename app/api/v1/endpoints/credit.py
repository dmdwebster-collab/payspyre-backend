from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.db.base import get_db
from app.models.credit import CreditInquiry, CreditReport
from app.models.loan import Borrower
from app.schemas.credit import (
    CreditInquiryRequest,
    CreditInquiryResponse,
    CreditPullResponse,
    CreditReportResponse,
)
from app.services.credit_bureau import CreditBureauService

router = APIRouter()
credit_service = CreditBureauService()


@router.post("/credit/pull", response_model=CreditPullResponse, status_code=status.HTTP_201_CREATED)
async def pull_credit_report(
    borrower_id: UUID,
    data: CreditInquiryRequest,
    db: Session = Depends(get_db),
):
    """Pull credit report from configured bureaus."""
    borrower = db.query(Borrower).filter(Borrower.id == borrower_id).first()
    if not borrower:
        raise HTTPException(status_code=404, detail="Borrower not found")

    date_of_birth_str = data.date_of_birth.strftime("%Y-%m-%d")

    bureaus_to_query = data.bureaus
    if not bureaus_to_query:
        if credit_service.equifax_client:
            bureaus_to_query = ["equifax"]
        elif credit_service.transunion_client:
            bureaus_to_query = ["transunion"]
        else:
            raise HTTPException(status_code=500, detail="No credit bureaus configured")

    # Create inquiry record
    inquiry = CreditInquiry(
        borrower_id=borrower_id,
        loan_application_id=data.loan_application_id,
        sin_last_3=data.sin_last_3,
        date_of_birth=data.date_of_birth,
        postal_code=data.postal_code,
        first_name=data.first_name,
        last_name=data.last_name,
        bureaus_queried=",".join(bureaus_to_query),
        use_cache="true" if data.use_cache else "false",
        status="pending",
    )
    db.add(inquiry)
    db.commit()
    db.refresh(inquiry)

    # Pull credit from bureaus
    try:
        result = await credit_service.pull_credit(
            sin_last_3=data.sin_last_3,
            date_of_birth=date_of_birth_str,
            postal_code=data.postal_code,
            first_name=data.first_name,
            last_name=data.last_name,
            bureaus=bureaus_to_query,
            use_cache=data.use_cache,
        )

        inquiry.status = "success"
        inquiry.cached = "false"

        # Store reports
        for bureau_name, report_data in result.get("reports", {}).items():
            credit_report = CreditReport(
                inquiry_id=inquiry.id,
                bureau=report_data.get("bureau"),
                score=report_data.get("score"),
                score_min=report_data.get("score_range", {}).get("min"),
                score_max=report_data.get("score_range", {}).get("max"),
                utilization_percent=report_data.get("utilization_percent"),
                delinquency_count=report_data.get("delinquency_count"),
                credit_history_months=report_data.get("credit_history_months"),
                trade_count=report_data.get("trade_count"),
                inquiry_count_6m=report_data.get("inquiry_count_6m"),
                inquiry_count_12m=report_data.get("inquiry_count_12m"),
                has_bankruptcy="true" if report_data.get("has_bankruptcy") else "false",
                has_collections="true" if report_data.get("has_collections") else "false",
                raw_response=report_data.get("raw_response"),
                fetched_at=datetime.fromisoformat(report_data.get("fetched_at")) if report_data.get("fetched_at") else None,
            )
            db.add(credit_report)

        # Update borrower with credit data
        aggregated = result.get("aggregated", {})
        if aggregated.get("average_score"):
            borrower.credit_score = aggregated["average_score"]
        if aggregated.get("average_history_months"):
            borrower.credit_history_months = aggregated["average_history_months"]

        db.commit()
        db.refresh(inquiry)

        return CreditPullResponse(
            inquiry_id=inquiry.id,
            status=inquiry.status,
            reports=[CreditReportResponse.model_validate(r) for r in inquiry.reports],
            aggregated=aggregated,
            errors=result.get("errors"),
            fetched_at=result.get("fetched_at", ""),
        )

    except Exception as e:
        inquiry.status = "failed"
        inquiry.error_message = str(e)
        db.commit()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/credit/inquiries/{inquiry_id}", response_model=CreditInquiryResponse)
async def get_credit_inquiry(inquiry_id: UUID, db: Session = Depends(get_db)):
    """Get credit inquiry details."""
    inquiry = db.query(CreditInquiry).filter(CreditInquiry.id == inquiry_id).first()
    if not inquiry:
        raise HTTPException(status_code=404, detail="Credit inquiry not found")
    return inquiry


@router.get("/credit/reports/{report_id}", response_model=CreditReportResponse)
async def get_credit_report(report_id: UUID, db: Session = Depends(get_db)):
    """Get credit report details."""
    report = db.query(CreditReport).filter(CreditReport.id == report_id).first()
    if not report:
        raise HTTPException(status_code=404, detail="Credit report not found")
    return report


@router.get("/borrowers/{borrower_id}/credit")
async def get_borrower_credit_history(
    borrower_id: UUID,
    skip: int = 0,
    limit: int = 10,
    db: Session = Depends(get_db),
):
    """Get credit inquiry history for a borrower."""
    inquiries = (
        db.query(CreditInquiry)
        .filter(CreditInquiry.borrower_id == borrower_id)
        .order_by(CreditInquiry.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )

    return [
        {
            "id": inquiry.id,
            "bureaus_queried": inquiry.bureaus_queried,
            "status": inquiry.status,
            "cached": inquiry.cached,
            "created_at": inquiry.created_at,
            "reports": [
                {
                    "bureau": report.bureau,
                    "score": report.score,
                    "utilization_percent": report.utilization_percent,
                    "delinquency_count": report.delinquency_count,
                    "credit_history_months": report.credit_history_months,
                }
                for report in inquiry.reports
            ],
        }
        for inquiry in inquiries
    ]


@router.get("/credit/recent/{borrower_id}")
async def get_recent_credit_report(
    borrower_id: UUID,
    hours: int = Query(24, description="Lookback period in hours"),
    db: Session = Depends(get_db),
):
    """Get most recent credit report within time window, if available."""
    from datetime import timedelta

    cutoff = datetime.utcnow() - timedelta(hours=hours)

    recent_inquiry = (
        db.query(CreditInquiry)
        .filter(CreditInquiry.borrower_id == borrower_id)
        .filter(CreditInquiry.created_at >= cutoff)
        .filter(CreditInquiry.status == "success")
        .order_by(CreditInquiry.created_at.desc())
        .first()
    )

    if not recent_inquiry:
        raise HTTPException(status_code=404, detail="No recent credit report found")

    return {
        "inquiry_id": recent_inquiry.id,
        "cached": recent_inquiry.cached,
        "created_at": recent_inquiry.created_at,
        "reports": [
            {
                "bureau": report.bureau,
                "score": report.score,
                "score_range": {"min": report.score_min, "max": report.score_max},
                "utilization_percent": report.utilization_percent,
                "delinquency_count": report.delinquency_count,
                "credit_history_months": report.credit_history_months,
                "has_bankruptcy": report.has_bankruptcy == "true",
                "has_collections": report.has_collections == "true",
            }
            for report in recent_inquiry.reports
        ],
    }