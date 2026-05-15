from datetime import datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.security import verify_webhook_signature
from app.core.config import settings
from app.db.base import get_db
from app.models.document import Document
from app.models.kyc import (
    KycEvent,
    KycResult,
    KycSession,
    KycCoBorrowerLink,
)
from app.schemas.document import DocumentUploadRequest, DocumentUploadResponse, DocumentConfirmUpload
from app.schemas.kyc import (
    CoBorrowerLinkRequest,
    CoBorrowerLinkResponse,
    DiditWebhookPayload,
    KycSessionCreate,
    KycSessionResponse,
    KycSessionStatus,
    KycWebhookResponse,
    PersonaWebhookPayload,
)
from app.services.kyc_vendor import get_vendor_client
from app.services.risk_engine import RiskRulesEngine
from app.services.underwriting_state_machine import state_machine
from app.services.storage import storage_service

router = APIRouter()
risk_engine = RiskRulesEngine()
limiter = Limiter(key_func=get_remote_address)


@router.post("/kyc/sessions", response_model=KycSessionResponse)
@limiter.limit("30/minute")
async def create_kyc_session(
    request: Request,
    data: KycSessionCreate,
    db: Session = Depends(get_db),
):
    vendor_client = get_vendor_client(data.vendor)
    session_id = uuid4()

    vendor_response = await vendor_client.create_verification_session(
        borrower_id=data.borrower_id,
        loan_application_id=data.loan_application_id,
        external_id=session_id,
    )

    db_session = KycSession(
        id=session_id,
        loan_application_id=data.loan_application_id,
        borrower_id=data.borrower_id,
        vendor=data.vendor,
        verification_url=vendor_response.verification_url,
        status="in_progress",
        expires_at=vendor_response.expires_at,
    )
    db.add(db_session)

    event = KycEvent(
        kyc_session_id=session_id,
        event_type="session_created",
        payload={
            "vendor": data.vendor,
            "verification_url": vendor_response.verification_url,
        },
    )
    db.add(event)

    db.commit()

    return vendor_response


@router.get("/kyc/sessions/{session_id}", response_model=KycSessionStatus)
@limiter.limit("100/minute")
async def get_kyc_session(request: Request, session_id: UUID, db: Session = Depends(get_db)):
    session = db.query(KycSession).filter(KycSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.post("/kyc/sessions/{session_id}/recreate", response_model=KycSessionResponse)
@limiter.limit("10/minute")
async def recreate_kyc_session(request: Request, session_id: UUID, db: Session = Depends(get_db)):
    session = db.query(KycSession).filter(KycSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    vendor_client = get_vendor_client(session.vendor)
    new_session_id = uuid4()

    vendor_response = await vendor_client.create_verification_session(
        borrower_id=session.borrower_id,
        loan_application_id=session.loan_application_id,
        external_id=new_session_id,
    )

    session.status = "expired"
    db.commit()

    new_session = KycSession(
        id=new_session_id,
        loan_application_id=session.loan_application_id,
        borrower_id=session.borrower_id,
        vendor=session.vendor,
        verification_url=vendor_response.verification_url,
        status="in_progress",
        expires_at=vendor_response.expires_at,
    )
    db.add(new_session)

    event = KycEvent(
        kyc_session_id=new_session_id,
        event_type="session_recreated",
        payload={"previous_session_id": str(session_id)},
    )
    db.add(event)

    db.commit()

    return vendor_response


@router.post("/kyc/webhooks/didit", response_model=KycWebhookResponse)
@limiter.limit("1000/minute")
async def didit_webhook(
    request: Request,
    db: Session = Depends(get_db),
):
    payload_bytes = await request.body()
    signature = request.headers.get("X-Didit-Signature", "")

    if not verify_webhook_signature(payload_bytes, signature, settings.DIDIT_WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = DiditWebhookPayload(**await request.json())

    if payload.event == "verification.completed":
        external_id = payload.data.get("external_id")
        session = db.query(KycSession).filter(KycSession.id == external_id).first()

        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        session.status = "completed"

        for check in payload.data.get("checks", []):
            result = KycResult(
                kyc_session_id=session.id,
                vendor="didit",
                overall_status=payload.data.get("status", "pass"),
                check_type=check.get("type"),
                check_status=check.get("status"),
                check_details=check.get("details"),
                score=check.get("score"),
                flags=check.get("flags", []),
            )
            db.add(result)

        event = KycEvent(
            kyc_session_id=session.id,
            event_type="webhook_received",
            payload=payload.dict(),
            vendor_event_id=payload.data.get("verification_id"),
        )
        db.add(event)

        db.commit()

        return KycWebhookResponse(received=True, kyc_session_id=session.id)

    return KycWebhookResponse(received=True)


@router.post("/kyc/webhooks/persona", response_model=KycWebhookResponse)
@limiter.limit("1000/minute")
async def persona_webhook(
    request: Request,
    db: Session = Depends(get_db),
):
    payload_bytes = await request.body()
    signature = request.headers.get("X-Persona-Signature", "")

    if not verify_webhook_signature(payload_bytes, signature, settings.PERSONA_WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = PersonaWebhookPayload(**await request.json())

    if payload.event == "inquiry.completed":
        external_id = payload.data.get("reference_id")
        session = db.query(KycSession).filter(KycSession.id == external_id).first()

        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        session.status = "completed"

        for check in payload.data.get("checks", []):
            result = KycResult(
                kyc_session_id=session.id,
                vendor="persona",
                overall_status=payload.data.get("status", "pass"),
                check_type=check.get("type"),
                check_status=check.get("status"),
                check_details=check.get("details"),
                score=check.get("score"),
                flags=check.get("flags", []),
            )
            db.add(result)

        event = KycEvent(
            kyc_session_id=session.id,
            event_type="webhook_received",
            payload=payload.dict(),
            vendor_event_id=payload.data.get("inquiry_id"),
        )
        db.add(event)

        db.commit()

        return KycWebhookResponse(received=True, kyc_session_id=session.id)

    return KycWebhookResponse(received=True)


@router.post("/kyc/evaluate")
@limiter.limit("20/minute")
async def evaluate_kyc_risk(
    request: Request,
    session_id: UUID,
    db: Session = Depends(get_db),
):
    session = db.query(KycSession).filter(KycSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    results = db.query(KycResult).filter(KycResult.kyc_session_id == session_id).all()

    kyc_data = {
        "checks": [
            {
                "type": result.check_type,
                "status": result.check_status,
                "details": result.check_details,
                "score": float(result.score) if result.score else None,
                "flags": result.flags or [],
            }
            for result in results
        ],
    }

    loan_app_data = {
        "loan_amount": 5000.0,
        "address": {"country": "CA"},
        "credit_history_months": 24,
    }

    evaluation = await risk_engine.evaluate(kyc_data, loan_app_data)

    return evaluation


@router.post("/kyc/co-borrowers/link", response_model=CoBorrowerLinkResponse)
@limiter.limit("30/minute")
async def link_co_borrower(
    request: Request,
    data: CoBorrowerLinkRequest,
    db: Session = Depends(get_db),
):
    link = KycCoBorrowerLink(
        loan_application_id=data.loan_application_id,
        primary_kyc_session_id=data.primary_kyc_session_id,
        co_borrower_kyc_session_id=data.co_borrower_kyc_session_id,
        co_borrower_role=data.co_borrower_role,
    )
    db.add(link)
    db.commit()
    db.refresh(link)

    return link


@router.post("/kyc/{session_id}/documents/upload/initiate", response_model=DocumentUploadResponse)
@limiter.limit("10/minute")
async def initiate_kyc_document_upload(
    request: Request,
    session_id: UUID,
    data: DocumentUploadRequest,
    db: Session = Depends(get_db),
):
    session = db.query(KycSession).filter(KycSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="KYC session not found")

    presigned_data = storage_service.generate_presigned_upload_url(
        entity_type="kyc_sessions",
        entity_id=str(session_id),
        document_type=data.document_type,
        filename=data.file_name,
        content_type=data.file_content_type,
        max_file_size_mb=10,
    )

    from app.models.document import Document
    from uuid import uuid4
    from datetime import datetime, timedelta, timezone

    document = Document(
        id=uuid4(),
        loan_application_id=session.loan_application_id,
        borrower_id=session.borrower_id,
        document_type=data.document_type,
        document_subtype=data.document_subtype,
        title=data.title,
        description=data.description or f"KYC document for session {session_id}",
        status="uploading",
        s3_object_key=presigned_data["object_key"],
        s3_bucket=storage_service._bucket_name,
        file_name=data.file_name,
        file_content_type=data.file_content_type,
        file_size_bytes=data.file_size_bytes,
        doc_metadata={
            "kyc_session_id": str(session_id),
            "vendor": session.vendor,
            "uploaded_via": "kyc_api",
            **(data.metadata or {}),
        },
        tags={"tags": data.tags} if data.tags else None,
        expires_at=datetime.now(timezone.utc) + timedelta(days=2555),
    )

    db.add(document)
    db.commit()
    db.refresh(document)

    return DocumentUploadResponse(
        upload_url=presigned_data["url"],
        upload_fields=presigned_data["fields"],
        document_id=document.id,
        object_key=presigned_data["object_key"],
        expires_in=presigned_data["expires_in"],
    )


@router.get("/kyc/{session_id}/documents")
@limiter.limit("100/minute")
async def list_kyc_documents(
    request: Request,
    session_id: UUID,
    db: Session = Depends(get_db),
):
    session = db.query(KycSession).filter(KycSession.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="KYC session not found")

    documents = db.query(Document).filter(
        Document.doc_metadata["kyc_session_id"].astext == str(session_id)
    ).order_by(Document.created_at.desc()).all()

    return {"documents": documents, "session_id": session_id, "total": len(documents)}