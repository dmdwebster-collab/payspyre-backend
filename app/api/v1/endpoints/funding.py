from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.db.base import get_db
from app.models.funding import Funding, Payment, PaymentSchedule, Refund, Statement
from app.models.loan import LoanApplication
from app.schemas.funding import (
    FundingRequest,
    FundingResponse,
    FundingStatusResponse,
    PaymentCreate,
    PaymentResponse,
    PaymentScheduleItem,
    RefundRequest,
    RefundResponse,
    StatementResponse,
)

router = APIRouter()
limiter = Limiter(key_func=get_remote_address)

LATE_FEE_RATE = Decimal("0.05")
LATE_FEE_GRACE_DAYS = 3
DAILY_INTEREST_RATE = Decimal("0.00027397")


def calculate_payment_schedule(
    principal: Decimal,
    annual_rate: Decimal,
    term_months: int,
    start_date: date,
    frequency: str,
) -> List[PaymentScheduleItem]:
    schedule = []

    if frequency == "weekly":
        periods = term_months * 4
        period_days = 7
    elif frequency == "bi_weekly":
        periods = term_months * 2
        period_days = 14
    elif frequency == "semi_monthly":
        periods = term_months * 2
        period_days = 15
    else:
        periods = term_months
        period_days = 30

    if periods == 0:
        periods = 1

    periodic_rate = annual_rate / Decimal(periods * 12)
    if frequency == "monthly":
        periodic_rate = annual_rate / Decimal(12)
    elif frequency == "weekly":
        periodic_rate = annual_rate / Decimal(52)
    elif frequency == "bi_weekly":
        periodic_rate = annual_rate / Decimal(26)
    elif frequency == "semi_monthly":
        periodic_rate = annual_rate / Decimal(24)

    payment_amount = principal * (periodic_rate * (1 + periodic_rate) ** periods) / ((1 + periodic_rate) ** periods - 1)
    payment_amount = payment_amount.quantize(Decimal("0.01"))

    remaining_balance = principal
    current_date = start_date

    for i in range(periods):
        if frequency == "semi_monthly" and i > 0:
            if i % 2 == 1:
                current_date = current_date.replace(day=15)
            else:
                if current_date.month == 12:
                    current_date = current_date.replace(year=current_date.year + 1, month=1, day=1)
                else:
                    current_date = current_date.replace(month=current_date.month + 1, day=1)
        else:
            current_date = current_date + timedelta(days=period_days)

        interest_payment = remaining_balance * periodic_rate
        interest_payment = interest_payment.quantize(Decimal("0.01"))

        principal_payment = payment_amount - interest_payment
        if i == periods - 1:
            principal_payment = remaining_balance
            payment_amount = principal_payment + interest_payment

        principal_payment = principal_payment.quantize(Decimal("0.01"))

        remaining_balance -= principal_payment
        if remaining_balance < 0:
            remaining_balance = Decimal("0")

        schedule.append(
            PaymentScheduleItem(
                payment_number=i + 1,
                due_date=current_date,
                payment_amount=payment_amount,
                principal_amount=principal_payment,
                interest_amount=interest_payment,
                remaining_balance=remaining_balance,
                is_paid=False,
            )
        )

    return schedule


def calculate_late_fee(scheduled_payment: PaymentScheduleItem, payment_date: date) -> Decimal:
    grace_period_end = scheduled_payment.due_date + timedelta(days=LATE_FEE_GRACE_DAYS)
    if payment_date > grace_period_end:
        return scheduled_payment.payment_amount * LATE_FEE_RATE
    return Decimal("0")


def calculate_interest_accrual(balance: Decimal, days: int) -> Decimal:
    return balance * DAILY_INTEREST_RATE * Decimal(days)


@router.post("/applications/{application_id}/fund", response_model=FundingResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
async def fund_application(request: Request, application_id: UUID, data: FundingRequest, db: Session = Depends(get_db)):
    application = db.query(LoanApplication).filter(LoanApplication.id == application_id).first()
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")

    if application.status != "approved":
        raise HTTPException(status_code=400, detail="Application must be approved before funding")

    existing_funding = db.query(Funding).filter(Funding.application_id == application_id).first()
    if existing_funding:
        raise HTTPException(status_code=400, detail="Application already funded")

    funding = Funding(
        application_id=application_id,
        disbursement_amount=data.disbursement_amount,
        disbursement_method=data.disbursement_method,
        vendor_account_number=data.vendor_account_number,
        vendor_institution_number=data.vendor_institution_number,
        vendor_transit_number=data.vendor_transit_number,
        disbursement_date=datetime.utcnow(),
        reference_number=f"FND-{application_id.hex[:8].upper()}",
        status="processing",
    )
    db.add(funding)

    schedule = calculate_payment_schedule(
        principal=Decimal(str(application.requested_amount)),
        annual_rate=application.interest_rate or Decimal("0.12"),
        term_months=int(application.term_months or 12),
        start_date=date.today(),
        frequency=application.payment_frequency or "monthly",
    )

    for item in schedule:
        schedule_item = PaymentSchedule(
            application_id=application_id,
            payment_number=item.payment_number,
            due_date=datetime.combine(item.due_date, datetime.min.time()),
            payment_amount=item.payment_amount,
            principal_amount=item.principal_amount,
            interest_amount=item.interest_amount,
            remaining_balance=item.remaining_balance,
            is_paid=False,
        )
        db.add(schedule_item)

    application.status = "funded"
    application.funded_at = datetime.utcnow()

    db.commit()
    db.refresh(funding)

    return funding


@router.get("/applications/{application_id}/funding-status", response_model=FundingStatusResponse)
@limiter.limit("100/minute")
async def get_funding_status(request: Request, application_id: UUID, db: Session = Depends(get_db)):
    application = db.query(LoanApplication).filter(LoanApplication.id == application_id).first()
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")

    funding = db.query(Funding).filter(Funding.application_id == application_id).first()
    if not funding:
        return FundingStatusResponse(
            application_id=application_id,
            funding_status="not_funded",
            disbursement_amount=None,
            disbursement_date=None,
            reference_number=None,
            funded_at=None,
        )

    return FundingStatusResponse(
        application_id=application_id,
        funding_status=funding.status,
        disbursement_amount=funding.disbursement_amount,
        disbursement_date=funding.disbursement_date,
        reference_number=funding.reference_number,
        funded_at=application.funded_at,
    )


@router.get("/payments/{payment_id}", response_model=PaymentResponse)
@limiter.limit("100/minute")
async def get_payment(request: Request, payment_id: UUID, db: Session = Depends(get_db)):
    payment = db.query(Payment).filter(Payment.id == payment_id).first()
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    return payment


@router.get("/applications/{application_id}/payments", response_model=List[PaymentResponse])
@limiter.limit("100/minute")
async def list_payments(
    request: Request,
    application_id: UUID,
    status: str | None = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    application = db.query(LoanApplication).filter(LoanApplication.id == application_id).first()
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")

    query = db.query(Payment).filter(Payment.application_id == application_id)

    if status:
        query = query.filter(Payment.status == status)

    payments = query.order_by(Payment.payment_date.desc()).offset(skip).limit(limit).all()
    return payments


@router.post("/payments", response_model=PaymentResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("20/minute")
async def record_payment(request: Request, data: PaymentCreate, db: Session = Depends(get_db)):
    application = db.query(LoanApplication).filter(LoanApplication.id == data.application_id).first()
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")

    schedule_items = db.query(PaymentSchedule).filter(
        PaymentSchedule.application_id == data.application_id,
        PaymentSchedule.is_paid == False,
    ).order_by(PaymentSchedule.due_date).all()

    if not schedule_items:
        raise HTTPException(status_code=400, detail="No pending payments found for this application")

    payment_date = data.payment_date or date.today()

    total_principal = Decimal("0")
    total_interest = Decimal("0")
    total_late_fees = Decimal("0")
    remaining_amount = data.amount

    for item in schedule_items:
        if remaining_amount <= 0:
            break

        late_fee = calculate_late_fee(
            PaymentScheduleItem(
                payment_number=item.payment_number,
                due_date=item.due_date.date(),
                payment_amount=item.payment_amount,
                principal_amount=item.principal_amount,
                interest_amount=item.interest_amount,
                remaining_balance=item.remaining_balance,
                is_paid=item.is_paid,
            ),
            payment_date,
        )

        principal_part = item.principal_amount
        interest_part = item.interest_amount

        if remaining_amount >= principal_part + interest_part + late_fee:
            remaining_amount -= principal_part + interest_part + late_fee
            total_principal += principal_part
            total_interest += interest_part
            total_late_fees += late_fee
            item.is_paid = True
        else:
            ratio = remaining_amount / (principal_part + interest_part + late_fee)
            total_principal += principal_part * ratio
            total_interest += interest_part * ratio
            total_late_fees += late_fee * ratio
            remaining_amount = Decimal("0")

    total_principal = total_principal.quantize(Decimal("0.01"))
    total_interest = total_interest.quantize(Decimal("0.01"))
    total_late_fees = total_late_fees.quantize(Decimal("0.01"))

    paid_amount = total_principal + total_interest + total_late_fees

    remaining_balance = Decimal("0")
    unpaid_items = db.query(PaymentSchedule).filter(
        PaymentSchedule.application_id == data.application_id,
        PaymentSchedule.is_paid == False,
    ).order_by(PaymentSchedule.due_date).first()
    if unpaid_items:
        remaining_balance = unpaid_items.remaining_balance

    payment = Payment(
        application_id=data.application_id,
        amount=paid_amount,
        payment_method=data.payment_method,
        payment_date=datetime.combine(payment_date, datetime.min.time()),
        transaction_id=data.transaction_id,
        status="completed",
        principal_amount=total_principal,
        interest_amount=total_interest,
        late_fee_amount=total_late_fees,
        remaining_balance=remaining_balance,
    )
    db.add(payment)

    if remaining_balance == Decimal("0"):
        application.status = "closed"

    db.commit()
    db.refresh(payment)

    return payment


@router.get("/statements/{statement_id}", response_model=StatementResponse)
@limiter.limit("100/minute")
async def get_statement(request: Request, statement_id: UUID, db: Session = Depends(get_db)):
    statement = db.query(Statement).filter(Statement.id == statement_id).first()
    if not statement:
        raise HTTPException(status_code=404, detail="Statement not found")
    return statement


@router.get("/applications/{application_id}/statements", response_model=List[StatementResponse])
@limiter.limit("100/minute")
async def list_statements(
    request: Request,
    application_id: UUID,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    application = db.query(LoanApplication).filter(LoanApplication.id == application_id).first()
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")

    statements = db.query(Statement).filter(Statement.application_id == application_id).order_by(
        Statement.statement_period_end.desc()
    ).offset(skip).limit(limit).all()

    return statements


@router.post("/payments/{payment_id}/refund", response_model=RefundResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
async def refund_payment(request: Request, payment_id: UUID, data: RefundRequest, db: Session = Depends(get_db)):
    payment = db.query(Payment).filter(Payment.id == payment_id).first()
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")

    if payment.status != "completed":
        raise HTTPException(status_code=400, detail="Only completed payments can be refunded")

    if data.amount > payment.amount:
        raise HTTPException(status_code=400, detail="Refund amount cannot exceed payment amount")

    existing_refund = db.query(Refund).filter(
        Refund.payment_id == payment_id,
        Refund.status == "completed",
    ).first()
    if existing_refund:
        raise HTTPException(status_code=400, detail="Payment already refunded")

    refund = Refund(
        payment_id=payment_id,
        amount=data.amount,
        reason=data.reason,
        refund_method=data.refund_method or "original_payment",
        status="processing",
        reference_number=f"REF-{payment_id.hex[:8].upper()}",
    )
    db.add(refund)

    if data.amount == payment.amount:
        payment.status = "refunded"
        payment.refunded_at = datetime.utcnow()

    db.commit()
    db.refresh(refund)

    return refund