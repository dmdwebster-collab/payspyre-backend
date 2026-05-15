import pytest
from uuid import uuid4
from datetime import datetime, date
from decimal import Decimal

from app.models.kyc import KycSession, KycResult
from app.models.loan import Borrower, LoanApplication, Vendor


@pytest.fixture
def setup_underwriting_data(db_session):
    vendor = Vendor(
        id=uuid4(),
        business_name="Test Dental",
        dba_name="Test Dental Care",
        business_type="corporation",
        contact_name="Dr. Smith",
        email="drsmith@test.com",
        phone="250-555-0123",
        address_line1="123 Dental St",
        city="Kelowna",
        province="BC",
        postal_code="V1V1V1",
    )
    db_session.add(vendor)

    borrower = Borrower(
        id=uuid4(),
        first_name="John",
        last_name="Doe",
        email="john.doe@example.com",
        phone="250-555-0199",
        date_of_birth=date(1990, 1, 15),
        address_line1="456 Main St",
        city="Kelowna",
        province="BC",
        postal_code="V2V2V2",
        country="CA",
        employment_income=Decimal("50000.00"),
    )
    db_session.add(borrower)

    # Create loan application first (required by FK)
    application = LoanApplication(
        id=uuid4(),
        borrower_id=borrower.id,
        vendor_id=vendor.id,
        requested_amount=Decimal("3000.00"),
        purpose="Dental implants",
        status="pending_documents",
    )
    db_session.add(application)
    db_session.flush()  # Flush to ensure application exists before creating FK references

    kyc_session = KycSession(
        id=uuid4(),
        loan_application_id=application.id,  # Use the application ID
        borrower_id=borrower.id,
        vendor="didit",
        verification_url="https://test.com/verify",
        status="completed",
    )
    db_session.add(kyc_session)

    kyc_result = KycResult(
        kyc_session_id=kyc_session.id,
        vendor="didit",
        overall_status="pass",
        check_type="identity",
        check_status="pass",
        check_details={"score": 0.98},
        score=Decimal("0.98"),
        flags=[],
    )
    db_session.add(kyc_result)

    kyc_result2 = KycResult(
        kyc_session_id=kyc_session.id,
        vendor="didit",
        overall_status="pass",
        check_type="liveness",
        check_status="pass",
        check_details={"score": 0.99},
        score=Decimal("0.99"),
        flags=[],
    )
    db_session.add(kyc_result2)

    kyc_result3 = KycResult(
        kyc_session_id=kyc_session.id,
        vendor="didit",
        overall_status="pass",
        check_type="aml",
        check_status="pass",
        check_details={"hits": []},
        flags=[],
    )
    db_session.add(kyc_result3)

    # Note: application already created above before kyc_session

    db_session.commit()

    return {
        "borrower": borrower,
        "application": application,
        "kyc_session": kyc_session,
        "vendor": vendor,
    }


@pytest.mark.asyncio
async def test_evaluate_application_approve(setup_underwriting_data, db_session):
    """Test evaluation with clean record should approve"""
    data = setup_underwriting_data
    application_id = data["application"].id

    from app.api.v1.endpoints.underwriting import evaluate_application

    result = await evaluate_application(application_id, db_session)

    assert result.decision == "approve"
    assert result.risk_score >= 0.85
    assert result.status == "approved"

    db_session.refresh(data["application"])
    assert data["application"].status == "approved"
    assert data["application"].decision == "approve"
    assert data["application"].approved_at is not None


@pytest.mark.asyncio
async def test_evaluate_application_requires_kyc(db_session):
    """Test evaluation fails without completed KYC"""
    from app.api.v1.endpoints.underwriting import evaluate_application

    vendor = Vendor(
        id=uuid4(),
        business_name="Test Dental",
        dba_name="Test Dental Care",
        business_type="corporation",
        contact_name="Dr. Smith",
        email="drsmith@test.com",
        phone="250-555-0123",
        address_line1="123 Dental St",
        city="Kelowna",
        province="BC",
        postal_code="V1V1V1",
    )
    db_session.add(vendor)

    borrower = Borrower(
        id=uuid4(),
        first_name="John",
        last_name="Doe",
        email="john.doe@example.com",
        phone="250-555-0199",
        date_of_birth=date(1990, 1, 15),
        address_line1="456 Main St",
        city="Kelowna",
        province="BC",
        postal_code="V2V2V2",
        country="CA",
    )
    db_session.add(borrower)

    application = LoanApplication(
        id=uuid4(),
        borrower_id=borrower.id,
        vendor_id=vendor.id,
        requested_amount=Decimal("3000.00"),
        status="pending_documents",
    )
    db_session.add(application)
    db_session.commit()

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc_info:
        await evaluate_application(application.id, db_session)

    assert exc_info.value.status_code == 400
    assert "No completed KYC sessions" in exc_info.value.detail


@pytest.mark.asyncio
async def test_manual_review_approve(setup_underwriting_data, db_session):
    """Test manual review can approve"""
    data = setup_underwriting_data
    application_id = data["application"].id

    # Put application in underwriting status first
    data["application"].status = "underwriting"
    db_session.commit()

    from app.api.v1.endpoints.underwriting import submit_manual_review
    from app.schemas.underwriting import UnderwritingManualReviewRequest

    request = UnderwritingManualReviewRequest(
        application_id=application_id,
        approved=True,
        notes="Credit score verified, good payment history",
    )

    result = await submit_manual_review(request, db_session)

    assert result.decision == "approve"
    assert result.status == "approved"
    assert "Credit score verified" in result.reason

    db_session.refresh(data["application"])
    assert data["application"].status == "approved"
    assert data["application"].approved_at is not None


@pytest.mark.asyncio
async def test_manual_review_reject(setup_underwriting_data, db_session):
    """Test manual review can reject"""
    data = setup_underwriting_data
    application_id = data["application"].id

    data["application"].status = "underwriting"
    db_session.commit()

    from app.api.v1.endpoints.underwriting import submit_manual_review
    from app.schemas.underwriting import UnderwritingManualReviewRequest

    request = UnderwritingManualReviewRequest(
        application_id=application_id,
        approved=False,
        notes="Insufficient income for requested amount",
    )

    result = await submit_manual_review(request, db_session)

    assert result.decision == "reject"
    assert result.status == "rejected"

    db_session.refresh(data["application"])
    assert data["application"].status == "rejected"


@pytest.mark.asyncio
async def test_get_underwriting_status(setup_underwriting_data, db_session):
    """Test getting underwriting status"""
    data = setup_underwriting_data
    application_id = data["application"].id

    from app.api.v1.endpoints.underwriting import get_underwriting_status

    result = await get_underwriting_status(application_id, db_session)

    assert result.application_id == application_id
    assert result.status == "pending_documents"
    assert result.decision is None


@pytest.mark.asyncio
async def test_request_rereview(setup_underwriting_data, db_session):
    """Test requesting re-review for rejected application"""
    data = setup_underwriting_data
    application_id = data["application"].id

    # Set application to rejected
    data["application"].status = "rejected"
    data["application"].decision = "reject"
    data["application"].decision_reason = "Automated rejection"
    db_session.commit()

    from app.api.v1.endpoints.underwriting import request_rereview

    result = await request_rereview(application_id, db_session)

    assert result.status == "underwriting"
    assert "Re-review requested" in result.decision_reason

    db_session.refresh(data["application"])
    assert data["application"].status == "underwriting"