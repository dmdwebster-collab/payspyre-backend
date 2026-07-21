"""Borrower portal — documents tab (WS-B; video 11 §1.6 redesign mandate).

Dave: the borrower "Customer documents" tab should hold the LOAN AGREEMENT,
the T&Cs and the PRIVACY POLICY (not a mirror of uploaded ID files). This
router serves exactly that:

* per-loan generated documents — the booking-time agreement snapshot (what the
  borrower signed/sees; immutable) + borrower-generated statements,
* the CURRENT terms-and-conditions / privacy-policy (highest active global
  template version, rendered),
* on-demand account statements (reuse of ``generate_statement`` figures;
  download-now — emailing lands when SendGrid creds do).

Auth + scoping mirror ``loans.py``: patient JWT, and a loan that isn't the
caller's is a 404 (never a 403 — we don't reveal other people's loans).
"""
from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.applicant.v1.deps import ApplicantClaims, get_current_applicant
from app.api.applicant.v1.endpoints.loans import _get_owned_loan
from app.db.base import get_db
from app.models.platform.document_template import PlatformLoanDocument
from app.services import document_engine
from app.services.document_engine import DocumentEngineError

router = APIRouter(tags=["borrower-documents"])

# What a borrower may see from the snapshot store: the booking-time agreement
# set + their own statements. Admin regenerations (generated_via='on_demand')
# are back-office working copies and stay internal.
_BORROWER_VISIBLE_VIA = ("booking", "borrower")


class BorrowerDocumentSummary(BaseModel):
    document_id: UUID
    kind: str
    title: str
    created_at: datetime


class BorrowerDocuments(BaseModel):
    documents: list[BorrowerDocumentSummary]


class LegalDocument(BaseModel):
    kind: str
    title: str
    version: int
    body_html: str


class StatementGenerateBody(BaseModel):
    period_start: date
    period_end: date


def _visible_docs_query(db: Session, loan_id: UUID):
    return (
        db.query(PlatformLoanDocument)
        .filter(
            PlatformLoanDocument.loan_id == loan_id,
            PlatformLoanDocument.generated_via.in_(_BORROWER_VISIBLE_VIA),
        )
    )


@router.get("/loans/{loan_id}/documents", response_model=BorrowerDocuments)
def list_loan_documents(
    loan_id: UUID,
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
) -> BorrowerDocuments:
    """The documents tab for one owned loan: agreement snapshot + statements."""
    _get_owned_loan(db, loan_id, claims.patient_id)  # 404 if not owned
    rows = (
        _visible_docs_query(db, loan_id)
        .order_by(PlatformLoanDocument.created_at.desc())
        .all()
    )
    return BorrowerDocuments(
        documents=[
            BorrowerDocumentSummary(
                document_id=d.id, kind=d.kind, title=d.title, created_at=d.created_at
            )
            for d in rows
        ]
    )


@router.get("/loans/{loan_id}/documents/{document_id}", response_class=HTMLResponse)
def download_loan_document(
    loan_id: UUID,
    document_id: UUID,
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
) -> HTMLResponse:
    """Download one document (the frozen rendered HTML)."""
    _get_owned_loan(db, loan_id, claims.patient_id)  # 404 if not owned
    d = (
        _visible_docs_query(db, loan_id)
        .filter(PlatformLoanDocument.id == document_id)
        .first()
    )
    if d is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
        )
    return HTMLResponse(
        content=d.body_html,
        headers={
            "Content-Disposition": f'attachment; filename="{d.kind}-{d.id}.html"'
        },
    )


@router.post(
    "/loans/{loan_id}/statements/generate",
    response_model=BorrowerDocumentSummary,
    status_code=status.HTTP_201_CREATED,
)
def generate_statement(
    loan_id: UUID,
    body: StatementGenerateBody,
    db: Session = Depends(get_db),
    claims: ApplicantClaims = Depends(get_current_applicant),
) -> BorrowerDocumentSummary:
    """On-demand account statement for a period (download via the documents
    list). Statement figures are idempotent per (loan, period); each request
    produces a fresh rendered snapshot."""
    loan = _get_owned_loan(db, loan_id, claims.patient_id)
    if body.period_end < body.period_start:
        raise HTTPException(status_code=422, detail="period_end must be >= period_start")
    try:
        doc = document_engine.generate_statement_document(
            db, loan, body.period_start, body.period_end, generated_via="borrower"
        )
    except DocumentEngineError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return BorrowerDocumentSummary(
        document_id=doc.id, kind=doc.kind, title=doc.title, created_at=doc.created_at
    )


def _legal_document(db: Session, kind: str) -> LegalDocument:
    template = document_engine.latest_active_template(db, kind)
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not available"
        )
    result = document_engine.render_standalone_document(template)
    return LegalDocument(
        kind=kind, title=template.title, version=template.version, body_html=result.html
    )


@router.get("/documents/terms-and-conditions", response_model=LegalDocument)
def current_terms(
    db: Session = Depends(get_db),
    _claims: ApplicantClaims = Depends(get_current_applicant),
) -> LegalDocument:
    """The CURRENT terms and conditions (highest active global version)."""
    return _legal_document(db, "terms_and_conditions")


@router.get("/documents/privacy-policy", response_model=LegalDocument)
def current_privacy_policy(
    db: Session = Depends(get_db),
    _claims: ApplicantClaims = Depends(get_current_applicant),
) -> LegalDocument:
    """The CURRENT privacy policy (highest active global version)."""
    return _legal_document(db, "privacy_policy")
