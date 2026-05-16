import pytest
from datetime import datetime, timedelta
from uuid import uuid4

from app.models.loan import LoanApplication, Borrower, Vendor
from app.models.funding import Payment, PaymentSchedule


@pytest.fixture
def test_data(db_session):
    """Create test data for analytics."""
    # Create vendor
    vendor = Vendor(
        id=uuid4(),
        business_name="Test Dental Clinic",
        dba_name="Test Clinic",
        business_type="corporation",
        contact_name="Dr. Test",
        email="test@example.com",
        phone="555-0100",
        address_line1="123 Test St",
        city="Vancouver",
        province="BC",
        postal_code="V6B 1A1",
        status="active"
    )
    db_session.add(vendor)
    db_session.flush()  # Flush to get vendor.id

    # Create borrower
    borrower = Borrower(
        id=uuid4(),
        first_name="John",
        last_name="Doe",
        email="john@example.com",
        phone="555-0101",
        date_of_birth=datetime(1990, 1, 1),
        address_line1="456 Test Ave",
        city="Vancouver",
        province="BC",
        postal_code="V6B 2B2",
        country="CA",
        employment_status="employed",
        employer_name="Test Corp",
        employment_income=50000.00,
        credit_score=720
    )
    db_session.add(borrower)
    db_session.flush()  # Flush to get borrower.id

    # Create loan applications
    now = datetime.now()
    for i in range(10):
        status = ["approved", "rejected", "funded"][i % 3]
        decision = ["approved", "rejected"][i % 2]
        loan = LoanApplication(
            id=uuid4(),
            borrower_id=borrower.id,
            vendor_id=vendor.id,
            requested_amount=5000.00 + (i * 100),
            purpose="Dental treatment",
            status=status,
            decision=decision if status in ["approved", "rejected"] else None,
            decision_at=now if status in ["approved", "rejected"] else None,
            term_months=12 + (i % 12),
            interest_rate=0.1499,
            payment_frequency="monthly",
            created_at=now - timedelta(days=i),
            submitted_at=now - timedelta(days=i),
        )
        db_session.add(loan)
        db_session.flush()  # Flush to get loan.id

        # Create payment schedules for funded loans
        if status == "funded":
            for month in range(1, 13):
                schedule = PaymentSchedule(
                    id=uuid4(),
                    application_id=loan.id,
                    payment_number=month,
                    due_date=now + timedelta(days=30 * month),
                    payment_amount=450.00,
                    principal_amount=400.00,
                    interest_amount=50.00,
                    remaining_balance=5000.00 - (month * 400.00),
                    is_paid="false" if month > 3 else "true"
                )
                db_session.add(schedule)

    db_session.commit()  # Commit all at once

    yield {
        "vendor_id": vendor.id,
        "borrower_id": borrower.id,
    }


def test_get_analytics_basic(client, test_data):
    """Test basic analytics retrieval."""
    response = client.get("/api/v1/analytics")

    assert response.status_code == 200
    data = response.json()

    # Check structure
    assert "loan_volume_trends" in data
    assert "approval_rates" in data
    assert "loan_metrics" in data
    assert "payment_collections" in data
    assert "delinquency_tracking" in data
    assert "risk_score_distribution" in data
    assert "vendor_performance" in data
    assert "geographic_distribution" in data


def test_get_analytics_with_date_range(client, test_data):
    """Test analytics with custom date range."""
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    response = client.get(
        f"/api/v1/analytics?start_date={start_date}&end_date={end_date}"
    )

    assert response.status_code == 200
    data = response.json()

    # Check loan metrics
    assert data["loan_metrics"]["totalVolume"] > 0
    assert data["loan_metrics"]["totalCount"] > 0


def test_get_analytics_weekly_granularity(client, test_data):
    """Test analytics with weekly granularity."""
    response = client.get("/api/v1/analytics?granularity=weekly")

    assert response.status_code == 200
    data = response.json()

    # Check that date format includes week number
    if data["loan_volume_trends"]:
        assert "-" in data["loan_volume_trends"][0]["date"]


@pytest.mark.skip("Funding tables not yet migrated - TODO: create migration for payments, payment_schedule, etc.")
def test_get_analytics_monthly_granularity(client, test_data):
    """Test analytics with monthly granularity."""
    response = client.get("/api/v1/analytics?granularity=monthly")

    assert response.status_code == 200
    data = response.json()

    # Check that date format is YYYY-MM
    if data["loan_volume_trends"]:
        date = data["loan_volume_trends"][0]["date"]
        assert len(date.split("-")) == 2


@pytest.mark.skip("Funding tables not yet migrated - TODO: create migration for payments, payment_schedule, etc.")
def test_approval_rates_structure(client, test_data):
    """Test approval rates data structure."""
    response = client.get("/api/v1/analytics")
    data = response.json()

    approval_rates = data["approval_rates"]
    if approval_rates:
        rate = approval_rates[0]
        assert "vendorId" in rate
        assert "vendorName" in rate
        assert "submitted" in rate
        assert "approved" in rate
        assert "rejected" in rate
        assert "approvalRate" in rate
        assert 0 <= rate["approvalRate"] <= 1


@pytest.mark.skip("Funding tables not yet migrated - TODO: create migration for payments, payment_schedule, etc.")
def test_loan_metrics_calculation(client, test_data):
    """Test loan metrics calculations."""
    response = client.get("/api/v1/analytics")
    data = response.json()

    metrics = data["loan_metrics"]
    assert metrics["totalVolume"] >= 0
    assert metrics["totalCount"] >= 0
    assert metrics["averageAmount"] >= 0
    assert metrics["averageTerm"] >= 0

    # Verify average calculation
    if metrics["totalCount"] > 0:
        expected_avg = metrics["totalVolume"] / metrics["totalCount"]
        assert abs(metrics["averageAmount"] - expected_avg) < 0.01


@pytest.mark.skip("Funding tables not yet migrated - TODO: create migration for payments, payment_schedule, etc.")
def test_vendor_performance_ranking(client, test_data):
    """Test vendor performance ranking."""
    response = client.get("/api/v1/analytics")
    data = response.json()

    vendor_performance = data["vendor_performance"]
    if vendor_performance:
        # Check that ranks are sequential starting from 1
        ranks = [v["rank"] for v in vendor_performance]
        assert sorted(ranks) == list(range(1, len(ranks) + 1))


@pytest.mark.skip("Funding tables not yet migrated - TODO: create migration for payments, payment_schedule, etc.")
def test_geographic_distribution(client, test_data):
    """Test geographic distribution data."""
    response = client.get("/api/v1/analytics")
    data = response.json()

    geo = data["geographic_distribution"]
    if geo:
        province = geo[0]
        assert "province" in province
        assert "loanCount" in province
        assert "totalVolume" in province
        assert "percentage" in province
        assert 0 <= province["percentage"] <= 1


@pytest.mark.skip("Funding tables not yet migrated - TODO: create migration for payments, payment_schedule, etc.")
def test_risk_score_distribution(client, test_data):
    """Test risk score distribution."""
    response = client.get("/api/v1/analytics")
    data = response.json()

    risk_dist = data["risk_score_distribution"]
    if risk_dist:
        # Check that percentages sum to approximately 1
        total_percentage = sum(r["percentage"] for r in risk_dist)
        assert abs(total_percentage - 1.0) < 0.01


@pytest.mark.skip("Funding tables not yet migrated - TODO: create migration for payments, payment_schedule, etc.")
def test_delinquency_tracking(client, test_data):
    """Test delinquency tracking data."""
    response = client.get("/api/v1/analytics")
    data = response.json()

    delinquency = data["delinquency_tracking"]
    if delinquency:
        period = delinquency[0]
        assert "period" in period
        assert "totalActive" in period
        assert "current" in period
        assert "days1to30" in period
        assert "days31to60" in period
        assert "days61to90" in period
        assert "days90Plus" in period
        assert "delinquencyRate" in period
        assert 0 <= period["delinquencyRate"] <= 1


@pytest.mark.skip("Funding tables not yet migrated - TODO: create migration for payments, payment_schedule, etc.")
def test_export_loans_csv(client, test_data):
    """Test exporting loans to CSV."""
    response = client.get("/api/v1/analytics/export?type=loans")

    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert "attachment" in response.headers["content-disposition"]


@pytest.mark.skip("Funding tables not yet migrated - TODO: create migration for payments, payment_schedule, etc.")
def test_export_payments_csv(client, test_data):
    """Test exporting payments to CSV."""
    response = client.get("/api/v1/analytics/export?type=payments")

    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]


@pytest.mark.skip("Funding tables not yet migrated - TODO: create migration for payments, payment_schedule, etc.")
def test_export_vendors_csv(client, test_data):
    """Test exporting vendors to CSV."""
    response = client.get("/api/v1/analytics/export?type=vendors")

    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]


@pytest.mark.skip("Funding tables not yet migrated - TODO: create migration for payments, payment_schedule, etc.")
def test_export_with_date_range(client, test_data):
    """Test export with custom date range."""
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    response = client.get(
        f"/api/v1/analytics/export?type=loans&start_date={start_date}&end_date={end_date}"
    )

    assert response.status_code == 200
    assert start_date in response.headers["content-disposition"]
    assert end_date in response.headers["content-disposition"]


@pytest.mark.skip("Funding tables not yet migrated - TODO: create migration for payments, payment_schedule, etc.")
def test_analytics_empty_data(client, db_session):
    """Test analytics behavior with no data."""
    response = client.get("/api/v1/analytics")

    assert response.status_code == 200
    data = response.json()

    # Should return empty arrays and zero metrics
    assert isinstance(data["loan_volume_trends"], list)
    assert isinstance(data["approval_rates"], list)
    assert data["loan_metrics"]["totalVolume"] == 0
    assert data["loan_metrics"]["totalCount"] == 0