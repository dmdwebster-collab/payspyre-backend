from datetime import datetime, date
from decimal import Decimal
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, and_
from sqlalchemy.orm import Session
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.db.base import get_db
from app.models.loan import Vendor, LoanApplication
from app.schemas.vendor import (
    VendorCreate,
    VendorUpdate,
    VendorResponse,
    VendorDetailResponse,
    VendorListResponse,
    ComplianceReviewCreate,
    VendorMetricsResponse,
)

router = APIRouter()
limiter = Limiter(key_func=get_remote_address)


@router.post("", response_model=VendorResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
async def create_vendor(request: Request, data: VendorCreate, db: Session = Depends(get_db)):
    existing = db.query(Vendor).filter(Vendor.email == data.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    vendor = Vendor(**data.model_dump())
    db.add(vendor)
    db.commit()
    db.refresh(vendor)

    return vendor


@router.get("/{vendor_id}", response_model=VendorDetailResponse)
@limiter.limit("100/minute")
async def get_vendor(request: Request, vendor_id: UUID, db: Session = Depends(get_db)):
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")
    return vendor


@router.get("", response_model=VendorListResponse)
@limiter.limit("100/minute")
async def list_vendors(
    request: Request,
    status: Optional[str] = Query(None, description="Filter by vendor status"),
    business_type: Optional[str] = Query(None, description="Filter by business type"),
    city: Optional[str] = Query(None, description="Filter by city"),
    province: Optional[str] = Query(None, description="Filter by province"),
    search: Optional[str] = Query(None, description="Search in business name, DBA, or contact name"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
    db: Session = Depends(get_db),
):
    query = db.query(Vendor)

    if status:
        query = query.filter(Vendor.status == status)
    if business_type:
        query = query.filter(Vendor.business_type == business_type)
    if city:
        query = query.filter(Vendor.city.ilike(f"%{city}%"))
    if province:
        query = query.filter(Vendor.province == province)
    if search:
        query = query.filter(
            (Vendor.business_name.ilike(f"%{search}%")) |
            (Vendor.dba_name.ilike(f"%{search}%")) |
            (Vendor.contact_name.ilike(f"%{search}%"))
        )

    total = query.count()
    vendors = query.order_by(Vendor.created_at.desc()).offset(skip).limit(limit).all()

    return VendorListResponse(
        vendors=vendors,
        total=total,
        skip=skip,
        limit=limit,
    )


@router.patch("/{vendor_id}", response_model=VendorResponse)
@limiter.limit("30/minute")
async def update_vendor(
    request: Request,
    vendor_id: UUID,
    data: VendorUpdate,
    db: Session = Depends(get_db),
):
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    if vendor.status == "terminated":
        raise HTTPException(status_code=400, detail="Cannot update terminated vendor")

    update_data = data.model_dump(exclude_unset=True)

    for field, value in update_data.items():
        setattr(vendor, field, value)

    db.commit()
    db.refresh(vendor)

    return vendor


@router.post("/{vendor_id}/compliance/review", response_model=VendorResponse)
@limiter.limit("10/minute")
async def submit_compliance_review(
    request: Request,
    vendor_id: UUID,
    data: ComplianceReviewCreate,
    db: Session = Depends(get_db),
):
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    if not data.license_verified:
        vendor.status = "suspended"
    else:
        compliance_status = await get_compliance_status(vendor_id, db)
        if compliance_status.compliance_score >= Decimal("80.00"):
            vendor.status = "active"
        else:
            vendor.status = "pending"

    vendor.last_reviewed_at = datetime.utcnow()
    vendor.compliance_score = (await get_compliance_status(vendor_id, db)).compliance_score

    if data.next_review_due:
        vendor.next_review_due = datetime.combine(data.next_review_due, datetime.min.time())

    db.commit()
    db.refresh(vendor)

    return vendor


@router.get("/{vendor_id}/metrics", response_model=VendorMetricsResponse)
@limiter.limit("100/minute")
async def get_vendor_metrics(request: Request, vendor_id: UUID, db: Session = Depends(get_db)):
    vendor = db.query(Vendor).filter(Vendor.id == vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    applications = db.query(LoanApplication).filter(LoanApplication.vendor_id == vendor_id).all()

    total_applications = len(applications)
    approved_applications = sum(1 for a in applications if a.decision == "approved")
    rejected_applications = sum(1 for a in applications if a.decision == "rejected")
    funded_applications = sum(1 for a in applications if a.status == "funded")

    total_funded_amount = sum(
        a.requested_amount for a in applications if a.status == "funded"
    ) or Decimal("0.00")

    average_funding_amount = (
        total_funded_amount / funded_applications
        if funded_applications > 0
        else Decimal("0.00")
    )

    approval_rate = (
        Decimal(approved_applications / total_applications * 100)
        if total_applications > 0
        else Decimal("0.00")
    )

    avg_decision_time = None
    decisions_with_times = [
        (a.decision_at, a.submitted_at)
        for a in applications
        if a.decision_at and a.submitted_at
    ]
    if decisions_with_times:
        total_hours = sum(
            (d[0] - d[1]).total_seconds() / 3600
            for d in decisions_with_times
        )
        avg_decision_time = Decimal(total_hours / len(decisions_with_times)).quantize(Decimal("0.01"))

    current_month_start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    current_month_apps = [
        a for a in applications
        if a.created_at and a.created_at >= current_month_start and a.status == "funded"
    ]
    current_month_volume = sum(a.requested_amount for a in current_month_apps) or Decimal("0.00")
    current_month_count = len(current_month_apps)

    last_activity = None
    if applications:
        last_activity = max(a.created_at for a in applications)

    return VendorMetricsResponse(
        vendor_id=vendor.id,
        business_name=vendor.business_name,
        total_applications=total_applications,
        approved_applications=approved_applications,
        rejected_applications=rejected_applications,
        funded_applications=funded_applications,
        total_funded_amount=total_funded_amount.quantize(Decimal("0.01")),
        average_funding_amount=average_funding_amount.quantize(Decimal("0.01")),
        approval_rate=approval_rate.quantize(Decimal("0.01")),
        average_decision_time_hours=avg_decision_time,
        current_month_volume=current_month_volume.quantize(Decimal("0.01")),
        current_month_count=current_month_count,
        last_activity_at=last_activity,
    )