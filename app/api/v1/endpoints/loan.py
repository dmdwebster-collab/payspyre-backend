from datetime import datetime
from typing import List
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.db.base import get_db
from app.models.loan import Borrower, LoanApplication, Vendor
from app.schemas.loan import (
    BorrowerCreate,
    BorrowerResponse,
    LoanApplicationCreate,
    LoanApplicationResponse,
    LoanApplicationUpdate,
)

router = APIRouter()
limiter = Limiter(key_func=get_remote_address)


@router.post("/borrowers", response_model=BorrowerResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
async def create_borrower(request: Request, data: BorrowerCreate, db: Session = Depends(get_db)):
    existing = db.query(Borrower).filter(Borrower.email == data.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    borrower = Borrower(**data.model_dump())
    db.add(borrower)
    db.commit()
    db.refresh(borrower)

    return borrower


@router.get("/borrowers/{borrower_id}", response_model=BorrowerResponse)
@limiter.limit("100/minute")
async def get_borrower(request: Request, borrower_id: UUID, db: Session = Depends(get_db)):
    borrower = db.query(Borrower).filter(Borrower.id == borrower_id).first()
    if not borrower:
        raise HTTPException(status_code=404, detail="Borrower not found")
    return borrower


@router.post("/applications", response_model=LoanApplicationResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
async def create_application(request: Request, data: LoanApplicationCreate, db: Session = Depends(get_db)):
    borrower = db.query(Borrower).filter(Borrower.id == data.borrower_id).first()
    if not borrower:
        raise HTTPException(status_code=404, detail="Borrower not found")

    vendor = db.query(Vendor).filter(Vendor.id == data.vendor_id).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor not found")

    if data.co_borrower_id:
        co_borrower = db.query(Borrower).filter(Borrower.id == data.co_borrower_id).first()
        if not co_borrower:
            raise HTTPException(status_code=404, detail="Co-borrower not found")

    application = LoanApplication(**data.model_dump())
    db.add(application)
    db.commit()
    db.refresh(application)

    return application


@router.get("/applications/{application_id}", response_model=LoanApplicationResponse)
@limiter.limit("100/minute")
async def get_application(request: Request, application_id: UUID, db: Session = Depends(get_db)):
    application = db.query(LoanApplication).filter(LoanApplication.id == application_id).first()
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")
    return application


@router.get("/applications", response_model=List[LoanApplicationResponse])
@limiter.limit("100/minute")
async def list_applications(
    request: Request,
    borrower_id: UUID | None = None,
    vendor_id: UUID | None = None,
    status: str | None = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    query = db.query(LoanApplication)

    if borrower_id:
        query = query.filter(LoanApplication.borrower_id == borrower_id)
    if vendor_id:
        query = query.filter(LoanApplication.vendor_id == vendor_id)
    if status:
        query = query.filter(LoanApplication.status == status)

    applications = query.offset(skip).limit(limit).all()
    return applications


@router.patch("/applications/{application_id}", response_model=LoanApplicationResponse)
@limiter.limit("30/minute")
async def update_application(
    request: Request,
    application_id: UUID,
    data: LoanApplicationUpdate,
    db: Session = Depends(get_db),
):
    application = db.query(LoanApplication).filter(LoanApplication.id == application_id).first()
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")

    update_data = data.model_dump(exclude_unset=True)

    if data.decision and not application.decision:
        application.decision_at = datetime.utcnow()

    if data.status == "approved" and not application.approved_at:
        application.approved_at = datetime.utcnow()

    for field, value in update_data.items():
        setattr(application, field, value)

    db.commit()
    db.refresh(application)

    return application


@router.post("/applications/{application_id}/submit", response_model=LoanApplicationResponse)
@limiter.limit("10/minute")
async def submit_application(request: Request, application_id: UUID, db: Session = Depends(get_db)):
    application = db.query(LoanApplication).filter(LoanApplication.id == application_id).first()
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")

    if application.status != "draft":
        raise HTTPException(status_code=400, detail="Only draft applications can be submitted")

    application.status = "pending_documents"
    application.submitted_at = datetime.utcnow()

    db.commit()
    db.refresh(application)

    return application