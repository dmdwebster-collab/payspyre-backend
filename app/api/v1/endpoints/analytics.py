from datetime import datetime, timedelta, date
from decimal import Decimal
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import func, and_, case, cast, Integer, extract
from sqlalchemy.orm import Session

from app.db.base import get_db
from app.models.loan import LoanApplication, Borrower, Vendor
from app.models.funding import Funding, Payment, PaymentSchedule

router = APIRouter()


@router.get("/")
async def get_analytics(
    start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    granularity: Optional[str] = Query("daily", description="daily, weekly, or monthly"),
    db: Session = Depends(get_db)
):
    """
    Get comprehensive analytics data for the lending platform.
    """

    if not start_date:
        start_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    if not end_date:
        end_date = datetime.now().strftime("%Y-%m-%d")

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    return {
        "loan_volume_trends": _get_loan_volume_trends(db, start_dt, end_dt, granularity),
        "approval_rates": _get_approval_rates(db, start_dt, end_dt),
        "loan_metrics": _get_loan_metrics(db, start_dt, end_dt),
        "payment_collections": _get_payment_collections(db, start_dt, end_dt, granularity),
        "delinquency_tracking": _get_delinquency_tracking(db, start_dt, end_dt, granularity),
        "risk_score_distribution": _get_risk_score_distribution(db, start_dt, end_dt),
        "vendor_performance": _get_vendor_performance(db, start_dt, end_dt),
        "geographic_distribution": _get_geographic_distribution(db, start_dt, end_dt),
    }


def _get_loan_volume_trends(
    db: Session, start_date: datetime, end_date: datetime, granularity: str
) -> list:
    if granularity == "daily":
        date_trunc = func.date_trunc('day', LoanApplication.created_at)
        date_format = func.to_char(LoanApplication.created_at, 'YYYY-MM-DD')
    elif granularity == "weekly":
        date_trunc = func.date_trunc('week', LoanApplication.created_at)
        date_format = func.to_char(LoanApplication.created_at, 'YYYY-"W"WW')
    else:  # monthly
        date_trunc = func.date_trunc('month', LoanApplication.created_at)
        date_format = func.to_char(LoanApplication.created_at, 'YYYY-MM')

    query = (
        db.query(
            date_format.label('date'),
            func.sum(LoanApplication.requested_amount).label('volume'),
            func.count(LoanApplication.id).label('count')
        )
        .filter(
            and_(
                LoanApplication.created_at >= start_date,
                LoanApplication.created_at <= end_date,
                LoanApplication.status.in_(['approved', 'funded', 'active', 'closed'])
            )
        )
        .group_by(date_trunc, date_format)
        .order_by(date_trunc)
        .all()
    )

    return [
        {
            "date": row.date,
            "volume": float(row.volume) if row.volume else 0.0,
            "count": row.count
        }
        for row in query
    ]


def _get_approval_rates(db: Session, start_date: datetime, end_date: datetime) -> list:
    query = (
        db.query(
            Vendor.id.label('vendorId'),
            Vendor.business_name.label('vendorName'),
            func.count(LoanApplication.id).label('submitted'),
            func.sum(case((LoanApplication.decision == 'approved', 1), else_=0)).label('approved'),
            func.sum(case((LoanApplication.decision == 'rejected', 1), else_=0)).label('rejected')
        )
        .join(LoanApplication, Vendor.id == LoanApplication.vendor_id)
        .filter(
            and_(
                LoanApplication.created_at >= start_date,
                LoanApplication.created_at <= end_date,
                LoanApplication.decision.in_(['approved', 'rejected'])
            )
        )
        .group_by(Vendor.id, Vendor.business_name)
        .all()
    )

    return [
        {
            "vendorId": str(row.vendorId),
            "vendorName": row.vendorName,
            "submitted": row.submitted,
            "approved": row.approved or 0,
            "rejected": row.rejected or 0,
            "approvalRate": (row.approved / row.submitted) if row.submitted > 0 else 0.0
        }
        for row in query
    ]


def _get_loan_metrics(db: Session, start_date: datetime, end_date: datetime) -> dict:
    query = (
        db.query(
            func.sum(LoanApplication.requested_amount).label('total_volume'),
            func.count(LoanApplication.id).label('total_count'),
            func.avg(LoanApplication.requested_amount).label('avg_amount'),
            func.avg(LoanApplication.term_months).label('avg_term')
        )
        .filter(
            and_(
                LoanApplication.created_at >= start_date,
                LoanApplication.created_at <= end_date,
                LoanApplication.status.in_(['approved', 'funded', 'active', 'closed'])
            )
        )
        .first()
    )

    return {
        "totalVolume": float(query.total_volume) if query.total_volume else 0.0,
        "totalCount": query.total_count or 0,
        "averageAmount": float(query.avg_amount) if query.avg_amount else 0.0,
        "averageTerm": float(query.avg_term) if query.avg_term else 0.0
    }


def _get_payment_collections(
    db: Session, start_date: datetime, end_date: datetime, granularity: str
) -> list:
    if granularity == "daily":
        date_trunc = func.date_trunc('day', Payment.payment_date)
        date_format = func.to_char(Payment.payment_date, 'YYYY-MM-DD')
    elif granularity == "weekly":
        date_trunc = func.date_trunc('week', Payment.payment_date)
        date_format = func.to_char(Payment.payment_date, 'YYYY-"W"WW')
    else:  # monthly
        date_trunc = func.date_trunc('month', Payment.payment_date)
        date_format = func.to_char(Payment.payment_date, 'YYYY-MM')

    # Get scheduled payments from payment_schedule
    scheduled_query = (
        db.query(
            date_format.label('period'),
            func.sum(PaymentSchedule.payment_amount).label('amount_scheduled'),
            func.count(PaymentSchedule.id).label('scheduled')
        )
        .filter(
            and_(
                PaymentSchedule.due_date >= start_date,
                PaymentSchedule.due_date <= end_date
            )
        )
        .group_by(date_trunc, date_format)
        .subquery()
    )

    # Get collected payments
    collected_query = (
        db.query(
            date_format.label('period'),
            func.sum(Payment.amount).label('amount_collected'),
            func.count(Payment.id).label('collected')
        )
        .filter(
            and_(
                Payment.payment_date >= start_date,
                Payment.payment_date <= end_date,
                Payment.status == 'completed'
            )
        )
        .group_by(date_trunc, date_format)
        .subquery()
    )

    # Join and calculate rates
    query = (
        db.query(
            scheduled_query.c.period,
            func.coalesce(scheduled_query.c.amount_scheduled, 0).label('amount_scheduled'),
            func.coalesce(scheduled_query.c.scheduled, 0).label('scheduled'),
            func.coalesce(collected_query.c.amount_collected, 0).label('amount_collected'),
            func.coalesce(collected_query.c.collected, 0).label('collected')
        )
        .outerjoin(collected_query, scheduled_query.c.period == collected_query.c.period)
        .order_by(scheduled_query.c.period)
        .all()
    )

    return [
        {
            "period": row.period,
            "scheduled": row.scheduled,
            "collected": row.collected,
            "amountScheduled": float(row.amount_scheduled),
            "amountCollected": float(row.amount_collected),
            "collectionRate": (row.amount_collected / row.amount_scheduled) if row.amount_scheduled > 0 else 0.0
        }
        for row in query
    ]


def _get_delinquency_tracking(
    db: Session, start_date: datetime, end_date: datetime, granularity: str
) -> list:
    if granularity == "daily":
        date_trunc = func.date_trunc('day', PaymentSchedule.due_date)
        date_format = func.to_char(PaymentSchedule.due_date, 'YYYY-MM-DD')
    elif granularity == "weekly":
        date_trunc = func.date_trunc('week', PaymentSchedule.due_date)
        date_format = func.to_char(PaymentSchedule.due_date, 'YYYY-"W"WW')
    else:  # monthly
        date_trunc = func.date_trunc('month', PaymentSchedule.due_date)
        date_format = func.to_char(PaymentSchedule.due_date, 'YYYY-MM')

    today = datetime.now()

    query = (
        db.query(
            date_format.label('period'),
            func.count(PaymentSchedule.id).label('total_active')
        )
        .filter(
            and_(
                PaymentSchedule.due_date >= start_date,
                PaymentSchedule.due_date <= end_date,
                PaymentSchedule.is_paid == False
            )
        )
        .group_by(date_trunc, date_format)
        .order_by(date_trunc)
        .all()
    )

    # Calculate delinquency buckets for each period
    result = []
    for row in query:
        days_overdue = extract('day', today - PaymentSchedule.due_date)
        current = 0
        days1to30 = 0
        days31to60 = 0
        days61to90 = 0
        days90_plus = 0

        # Get payment schedules for this period and categorize by days overdue
        period_schedules = (
            db.query(PaymentSchedule)
            .filter(
                and_(
                    func.to_char(PaymentSchedule.due_date, date_format == row.period),
                    PaymentSchedule.is_paid == False
                )
            )
            .all()
        )

        for schedule in period_schedules:
            overdue = (today - schedule.due_date).days
            if overdue <= 0:
                current += 1
            elif overdue <= 30:
                days1to30 += 1
            elif overdue <= 60:
                days31to60 += 1
            elif overdue <= 90:
                days61to90 += 1
            else:
                days90_plus += 1

        total_delinquent = days1to30 + days31to60 + days61to90 + days90_plus
        delinquency_rate = total_delinquent / row.total_active if row.total_active > 0 else 0.0

        result.append({
            "period": row.period,
            "totalActive": row.total_active,
            "current": current,
            "days1to30": days1to30,
            "days31to60": days31to60,
            "days61to90": days61to90,
            "days90Plus": days90_plus,
            "delinquencyRate": delinquency_rate
        })

    return result


def _get_risk_score_distribution(db: Session, start_date: datetime, end_date: datetime) -> list:
    # Define risk score buckets
    query = (
        db.query(
            case(
                (Borrower.credit_score >= 750, '750+ (Excellent)'),
                (Borrower.credit_score >= 700, '700-749 (Good)'),
                (Borrower.credit_score >= 650, '650-699 (Fair)'),
                (Borrower.credit_score >= 600, '600-649 (Poor)'),
                else_='Below 600 (Very Poor)'
            ).label('score_range'),
            func.count(LoanApplication.id).label('count')
        )
        .join(Borrower, LoanApplication.borrower_id == Borrower.id)
        .filter(
            and_(
                LoanApplication.created_at >= start_date,
                LoanApplication.created_at <= end_date,
                Borrower.credit_score.isnot(None)
            )
        )
        .group_by('score_range')
        .order_by(func.count(LoanApplication.id).desc())
        .all()
    )

    total_loans = sum(row.count for row in query)

    return [
        {
            "scoreRange": row.score_range,
            "count": row.count,
            "percentage": (row.count / total_loans) if total_loans > 0 else 0.0
        }
        for row in query
    ]


def _get_vendor_performance(db: Session, start_date: datetime, end_date: datetime) -> list:
    # Calculate vendor performance metrics
    query = (
        db.query(
            Vendor.id.label('vendorId'),
            Vendor.business_name.label('vendorName'),
            func.count(LoanApplication.id).label('loan_count'),
            func.sum(LoanApplication.requested_amount).label('total_volume'),
            func.avg(LoanApplication.requested_amount).label('avg_amount')
        )
        .join(LoanApplication, Vendor.id == LoanApplication.vendor_id)
        .filter(
            and_(
                LoanApplication.created_at >= start_date,
                LoanApplication.created_at <= end_date
            )
        )
        .group_by(Vendor.id, Vendor.business_name)
        .all()
    )

    result = []
    for row in query:
        # Get approval rate for this vendor
        approval_query = (
            db.query(
                func.sum(case((LoanApplication.decision == 'approved', 1), else_=0)).label('approved'),
                func.count(LoanApplication.id).label('total')
            )
            .filter(
                and_(
                    LoanApplication.vendor_id == row.vendorId,
                    LoanApplication.created_at >= start_date,
                    LoanApplication.created_at <= end_date,
                    LoanApplication.decision.in_(['approved', 'rejected'])
                )
            )
            .first()
        )

        approval_rate = (approval_query.approved / approval_query.total) if approval_query.total > 0 else 0.0

        # Get collection rate for this vendor
        collection_query = (
            db.query(
                func.sum(case((Payment.status == 'completed', 1), else_=0)).label('collected'),
                func.count(Payment.id).label('total')
            )
            .join(LoanApplication, Payment.application_id == LoanApplication.id)
            .filter(
                and_(
                    LoanApplication.vendor_id == row.vendorId,
                    Payment.payment_date >= start_date,
                    Payment.payment_date <= end_date
                )
            )
            .first()
        )

        collection_rate = (collection_query.collected / collection_query.total) if collection_query.total > 0 else 0.0

        # Get delinquency rate for this vendor
        delinquency_query = (
            db.query(
                func.sum(case((PaymentSchedule.is_paid == False, 1), else_=0)).label('delinquent'),
                func.count(PaymentSchedule.id).label('total')
            )
            .join(LoanApplication, PaymentSchedule.application_id == LoanApplication.id)
            .filter(
                and_(
                    LoanApplication.vendor_id == row.vendorId,
                    PaymentSchedule.due_date <= datetime.now(),
                    PaymentSchedule.due_date >= start_date
                )
            )
            .first()
        )

        delinquency_rate = (delinquency_query.delinquent / delinquency_query.total) if delinquency_query.total > 0 else 0.0

        result.append({
            "vendorId": str(row.vendorId),
            "vendorName": row.vendorName,
            "loanCount": row.loan_count,
            "totalVolume": float(row.total_volume) if row.total_volume else 0.0,
            "averageLoanAmount": float(row.avg_amount) if row.avg_amount else 0.0,
            "approvalRate": approval_rate,
            "collectionRate": collection_rate,
            "delinquencyRate": delinquency_rate,
            "rank": 0  # Will be calculated after sorting
        })

    # Sort by performance score (combination of volume, approval rate, collection rate, and inverse delinquency)
    result.sort(
        key=lambda x: (
            x.totalVolume * 0.4 +
            x.approvalRate * x.loan_count * 0.3 +
            x.collectionRate * x.loan_count * 0.2 +
            (1 - x.delinquencyRate) * x.loan_count * 0.1
        ),
        reverse=True
    )

    # Assign ranks
    for i, vendor in enumerate(result, 1):
        vendor['rank'] = i

    return result


def _get_geographic_distribution(db: Session, start_date: datetime, end_date: datetime) -> list:
    query = (
        db.query(
            Borrower.province.label('province'),
            func.count(LoanApplication.id).label('loan_count'),
            func.sum(LoanApplication.requested_amount).label('total_volume'),
            func.avg(LoanApplication.requested_amount).label('avg_amount')
        )
        .join(LoanApplication, Borrower.id == LoanApplication.borrower_id)
        .filter(
            and_(
                LoanApplication.created_at >= start_date,
                LoanApplication.created_at <= end_date
            )
        )
        .group_by(Borrower.province)
        .order_by(func.count(LoanApplication.id).desc())
        .all()
    )

    total_loans = sum(row.loan_count for row in query)

    return [
        {
            "province": row.province,
            "loanCount": row.loan_count,
            "totalVolume": float(row.total_volume) if row.total_volume else 0.0,
            "averageLoanAmount": float(row.avg_amount) if row.avg_amount else 0.0,
            "percentage": (row.loan_count / total_loans) if total_loans > 0 else 0.0
        }
        for row in query
    ]


@router.get("/export")
async def export_analytics(
    start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    type: Optional[str] = Query("loans", description="loans, payments, or vendors"),
    db: Session = Depends(get_db)
):
    """
    Export analytics data to CSV format.
    """

    if not start_date:
        start_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    if not end_date:
        end_date = datetime.now().strftime("%Y-%m-%d")

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")

    import csv
    import io

    output = io.StringIO()
    writer = csv.writer(output)

    if type == "loans":
        writer.writerow([
            "Loan ID", "Borrower Name", "Vendor Name", "Amount", "Status",
            "Decision", "Created At", "Approved At", "Funded At"
        ])

        query = (
            db.query(
                LoanApplication.id,
                Borrower.first_name,
                Borrower.last_name,
                Vendor.business_name,
                LoanApplication.requested_amount,
                LoanApplication.status,
                LoanApplication.decision,
                LoanApplication.created_at,
                LoanApplication.approved_at,
                LoanApplication.funded_at
            )
            .join(Borrower, LoanApplication.borrower_id == Borrower.id)
            .join(Vendor, LoanApplication.vendor_id == Vendor.id)
            .filter(
                and_(
                    LoanApplication.created_at >= start_dt,
                    LoanApplication.created_at <= end_dt
                )
            )
            .all()
        )

        for row in query:
            writer.writerow([
                str(row.id),
                f"{row.first_name} {row.last_name}",
                row.business_name,
                float(row.requested_amount),
                row.status,
                row.decision,
                row.created_at.isoformat() if row.created_at else "",
                row.approved_at.isoformat() if row.approved_at else "",
                row.funded_at.isoformat() if row.funded_at else ""
            ])

    elif type == "payments":
        writer.writerow([
            "Payment ID", "Loan ID", "Amount", "Payment Date", "Status",
            "Payment Method", "Principal", "Interest", "Late Fees"
        ])

        query = (
            db.query(
                Payment.id,
                Payment.application_id,
                Payment.amount,
                Payment.payment_date,
                Payment.status,
                Payment.payment_method,
                Payment.principal_amount,
                Payment.interest_amount,
                Payment.late_fee_amount
            )
            .filter(
                and_(
                    Payment.payment_date >= start_dt,
                    Payment.payment_date <= end_dt
                )
            )
            .all()
        )

        for row in query:
            writer.writerow([
                str(row.id),
                str(row.application_id),
                float(row.amount),
                row.payment_date.isoformat() if row.payment_date else "",
                row.status,
                row.payment_method,
                float(row.principal_amount),
                float(row.interest_amount),
                float(row.late_fee_amount)
            ])

    elif type == "vendors":
        writer.writerow([
            "Vendor ID", "Business Name", "DBA Name", "Status",
            "Loan Count", "Total Volume", "Approval Rate", "Collection Rate"
        ])

        query = (
            db.query(
                Vendor.id,
                Vendor.business_name,
                Vendor.dba_name,
                Vendor.status,
                func.count(LoanApplication.id).label('loan_count'),
                func.sum(LoanApplication.requested_amount).label('total_volume')
            )
            .outerjoin(LoanApplication, Vendor.id == LoanApplication.vendor_id)
            .filter(
                and_(
                    LoanApplication.created_at >= start_dt,
                    LoanApplication.created_at <= end_dt
                )
            )
            .group_by(Vendor.id, Vendor.business_name, Vendor.dba_name, Vendor.status)
            .all()
        )

        for row in query:
            writer.writerow([
                str(row.id),
                row.business_name,
                row.dba_name or "",
                row.status,
                row.loan_count,
                float(row.total_volume) if row.total_volume else 0.0,
                "",  # Approval rate would need separate calculation
                ""   # Collection rate would need separate calculation
            ])

    output.seek(0)
    return StreamingResponse(
        io.BytesIO(output.getvalue().encode('utf-8')),
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=payspyre-{type}-{start_date}-to-{end_date}.csv"
        }
    )