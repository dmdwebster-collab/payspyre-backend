import pytest
from unittest.mock import Mock, patch
from uuid import uuid4
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from app.models.document import Document
from app.models.loan import Borrower, Vendor, LoanApplication
from app.schemas.document import DocumentUploadRequest
from app.services.storage import StorageService


@pytest.fixture
def mock_s3_client():
    with patch("app.services.storage.boto3.client") as mock_client:
        s3_mock = Mock()
        mock_client.return_value = s3_mock
        yield s3_mock


@pytest.fixture
def mock_s3_resource():
    with patch("app.services.storage.boto3.resource") as mock_resource:
        resource_mock = Mock()
        mock_resource.return_value = resource_mock
        yield resource_mock


@pytest.fixture
def storage_service(mock_s3_client):
    """Create a storage service with mocked S3 client."""
    service = StorageService()
    # Manually initialize with mocked client
    service._s3_client = mock_s3_client
    service._bucket_name = "test-bucket"
    service._region = "us-east-1"
    service._initialized = True
    return service


def test_storage_service_generate_presigned_upload_url(storage_service, mock_s3_client):
    mock_s3_client.generate_presigned_post.return_value = {
        "url": "https://s3.amazonaws.com/bucket",
        "fields": {"key": "value"},
    }

    result = storage_service.generate_presigned_upload_url(
        entity_type="kyc_sessions",
        entity_id="123",
        document_type="id_front",
        filename="test.jpg",
        content_type="image/jpeg",
    )

    assert "url" in result
    assert "fields" in result
    assert "object_key" in result
    assert result["expires_in"] == 3600


def test_storage_service_generate_presigned_download_url(storage_service, mock_s3_client):
    mock_s3_client.generate_presigned_url.return_value = "https://s3.amazonaws.com/bucket/key?signature=xxx"

    url = storage_service.generate_presigned_download_url("kyc_sessions/123/id_front/test.jpg")

    assert url.startswith("https://")


def test_storage_service_get_object_metadata(storage_service, mock_s3_client):
    mock_s3_client.head_object.return_value = {
        "ContentType": "image/jpeg",
        "ContentLength": 1024,
        "LastModified": datetime.now(timezone.utc),
        "Metadata": {"custom": "value"},
        "ETag": '"abc123"',
    }

    metadata = storage_service.get_object_metadata("kyc_sessions/123/id_front/test.jpg")

    assert metadata["content_type"] == "image/jpeg"
    assert metadata["content_length"] == 1024
    assert metadata["etag"] == '"abc123"'


def test_storage_service_verify_object_exists(storage_service, mock_s3_client):
    mock_s3_client.head_object.return_value = {}

    exists = storage_service.verify_object_exists("kyc_sessions/123/id_front/test.jpg")

    assert exists is True


def test_storage_service_verify_object_not_found(storage_service, mock_s3_client):
    from botocore.exceptions import ClientError

    error_response = {"Error": {"Code": "404", "Message": "Not Found"}}
    mock_s3_client.head_object.side_effect = ClientError(error_response, "HeadObject")

    exists = storage_service.verify_object_exists("nonexistent.jpg")

    assert exists is False


@pytest.mark.asyncio
async def test_initiate_document_upload(client, db_session):
    # Create test data with proper FK relationships
    vendor = Vendor(
        id=uuid4(),
        business_name="Test Vendor",
        business_type="sole_proprietor",
        contact_name="Test Contact",
        email="vendor@example.com",
        phone="555-1234",
        address_line1="123 Test St",
        city="Test City",
        province="BC",
        postal_code="V1V 1V1",
    )
    db_session.add(vendor)
    db_session.flush()

    borrower = Borrower(
        id=uuid4(),
        first_name="Test",
        last_name="Borrower",
        email="borrower@example.com",
        phone="555-5678",
        date_of_birth=datetime.now(timezone.utc),
        address_line1="123 Borrower St",
        city="Borrower City",
        province="BC",
        postal_code="V1V 1V1",
    )
    db_session.add(borrower)
    db_session.flush()

    loan_app = LoanApplication(
        id=uuid4(),
        borrower_id=borrower.id,
        vendor_id=vendor.id,
        requested_amount=1000.00,
    )
    db_session.add(loan_app)
    db_session.commit()

    # Use environment variables to mock S3 credentials
    import os
    old_access_key = os.environ.get("AWS_ACCESS_KEY_ID")
    old_secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
    old_bucket = os.environ.get("AWS_S3_BUCKET")

    os.environ["AWS_ACCESS_KEY_ID"] = "test"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "test"
    os.environ["AWS_S3_BUCKET"] = "test-bucket"
    os.environ["AWS_REGION"] = "us-east-1"

    # Reinitialize the storage service
    from app.services.storage import storage_service as ss
    from unittest.mock import patch, MagicMock

    # Create a complete mock S3 client
    mock_s3_client = MagicMock()
    mock_s3_client.generate_presigned_post.return_value = {
        "url": "https://s3.amazonaws.com/test",
        "fields": {"key": "value"},
    }

    # Patch the boto3 client creation
    with patch("boto3.client", return_value=mock_s3_client):
        # Force reinitialization
        ss._initialized = False
        ss._initialize()

        try:
            request_data = DocumentUploadRequest(
                document_type="id_front",
                title="Test ID",
                file_name="test.jpg",
                file_content_type="image/jpeg",
                loan_application_id=str(loan_app.id),
            )

            response = client.post("/api/v1/documents/documents/upload/initiate", json=request_data.model_dump(mode='json'))

            assert response.status_code == 200
            data = response.json()
            assert "upload_url" in data
            assert "document_id" in data
        finally:
            # Restore environment variables
            if old_access_key:
                os.environ["AWS_ACCESS_KEY_ID"] = old_access_key
            else:
                os.environ.pop("AWS_ACCESS_KEY_ID", None)
            if old_secret_key:
                os.environ["AWS_SECRET_ACCESS_KEY"] = old_secret_key
            else:
                os.environ.pop("AWS_SECRET_ACCESS_KEY", None)
            if old_bucket:
                os.environ["AWS_S3_BUCKET"] = old_bucket
            else:
                os.environ.pop("AWS_S3_BUCKET", None)

        request_data = DocumentUploadRequest(
            document_type="id_front",
            title="Test ID",
            file_name="test.jpg",
            file_content_type="image/jpeg",
            loan_application_id=str(loan_app.id),
        )

        response = client.post("/api/v1/documents/documents/upload/initiate", json=request_data.model_dump(mode='json'))

        assert response.status_code == 200
        data = response.json()
        assert "upload_url" in data
        assert "document_id" in data


@pytest.mark.asyncio
async def test_confirm_document_upload(client, db_session, mock_s3_client):
    # Mock the storage service's S3 client, bucket name and methods
    from app.services.storage import storage_service
    from unittest.mock import MagicMock

    original_s3_client = storage_service._s3_client
    original_bucket = getattr(storage_service, '_bucket_name', None)

    # Create a mock S3 client
    mock_s3 = MagicMock()
    mock_s3.head_object.return_value = {
        "ContentType": "image/jpeg",
        "ContentLength": 1024,
        "ETag": '"abc123"',
    }
    storage_service._s3_client = mock_s3
    storage_service._bucket_name = "test-bucket"

    try:

        # Create test data with proper FK relationships
        vendor = Vendor(
            id=uuid4(),
            business_name="Test Vendor",
            business_type="sole_proprietor",
            contact_name="Test Contact",
            email="vendor@example.com",
            phone="555-1234",
            address_line1="123 Test St",
            city="Test City",
            province="BC",
            postal_code="V1V 1V1",
        )
        db_session.add(vendor)
        db_session.flush()

        borrower = Borrower(
            id=uuid4(),
            first_name="Test",
            last_name="Borrower",
            email="borrower@example.com",
            phone="555-5678",
            date_of_birth=datetime.now(timezone.utc),
            address_line1="123 Borrower St",
            city="Borrower City",
            province="BC",
            postal_code="V1V 1V1",
        )
        db_session.add(borrower)
        db_session.flush()

        loan_app = LoanApplication(
            id=uuid4(),
            borrower_id=borrower.id,
            vendor_id=vendor.id,
            requested_amount=1000.00,
        )
        db_session.add(loan_app)
        db_session.flush()

        document = Document(
            id=uuid4(),
            loan_application_id=loan_app.id,
            document_type="id_front",
            title="Test ID",
            status="uploading",
            s3_object_key="test.jpg",
            s3_bucket="test-bucket",
            file_name="test.jpg",
            expires_at=datetime.now(timezone.utc) + timedelta(days=2555),
        )
        db_session.add(document)
        db_session.commit()

        response = client.post(
            "/api/v1/documents/documents/upload/confirm",
            json={"document_id": str(document.id), "file_size_bytes": 1024},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "uploaded"
        assert data["file_size_bytes"] == 1024
    finally:
        # Restore original S3 client and bucket name
        storage_service._s3_client = original_s3_client
        if original_bucket:
            storage_service._bucket_name = original_bucket
        elif hasattr(storage_service, '_bucket_name'):
            delattr(storage_service, '_bucket_name')


@pytest.mark.asyncio
async def test_get_document(client, db_session):
    # Create test data with proper FK relationships
    vendor = Vendor(
        id=uuid4(),
        business_name="Test Vendor",
        business_type="sole_proprietor",
        contact_name="Test Contact",
        email="vendor@example.com",
        phone="555-1234",
        address_line1="123 Test St",
        city="Test City",
        province="BC",
        postal_code="V1V 1V1",
    )
    db_session.add(vendor)
    db_session.flush()

    borrower = Borrower(
        id=uuid4(),
        first_name="Test",
        last_name="Borrower",
        email="borrower@example.com",
        phone="555-5678",
        date_of_birth=datetime.now(timezone.utc),
        address_line1="123 Borrower St",
        city="Borrower City",
        province="BC",
        postal_code="V1V 1V1",
    )
    db_session.add(borrower)
    db_session.flush()

    loan_app = LoanApplication(
        id=uuid4(),
        borrower_id=borrower.id,
        vendor_id=vendor.id,
        requested_amount=1000.00,
    )
    db_session.add(loan_app)
    db_session.flush()

    document = Document(
        id=uuid4(),
        loan_application_id=loan_app.id,
        document_type="id_front",
        title="Test ID",
        status="uploaded",
        s3_object_key="test.jpg",
        s3_bucket="test-bucket",
        file_name="test.jpg",
        expires_at=datetime.now(timezone.utc) + timedelta(days=2555),
    )
    db_session.add(document)
    db_session.commit()

    response = client.get(f"/api/v1/documents/documents/{document.id}")

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(document.id)
    assert data["title"] == "Test ID"


@pytest.mark.asyncio
async def test_list_documents(client, db_session):
    # Create test data with proper FK relationships
    vendor = Vendor(
        id=uuid4(),
        business_name="Test Vendor",
        business_type="sole_proprietor",
        contact_name="Test Contact",
        email="vendor@example.com",
        phone="555-1234",
        address_line1="123 Test St",
        city="Test City",
        province="BC",
        postal_code="V1V 1V1",
    )
    db_session.add(vendor)
    db_session.flush()

    borrower = Borrower(
        id=uuid4(),
        first_name="Test",
        last_name="Borrower",
        email="borrower@example.com",
        phone="555-5678",
        date_of_birth=datetime.now(timezone.utc),
        address_line1="123 Borrower St",
        city="Borrower City",
        province="BC",
        postal_code="V1V 1V1",
    )
    db_session.add(borrower)
    db_session.flush()

    loan_app = LoanApplication(
        id=uuid4(),
        borrower_id=borrower.id,
        vendor_id=vendor.id,
        requested_amount=1000.00,
    )
    db_session.add(loan_app)
    db_session.flush()

    for i in range(3):
        document = Document(
            id=uuid4(),
            loan_application_id=loan_app.id,
            document_type="id_front",
            title=f"Test ID {i}",
            status="uploaded",
            s3_object_key=f"test{i}.jpg",
            s3_bucket="test-bucket",
            file_name=f"test{i}.jpg",
            expires_at=datetime.now(timezone.utc) + timedelta(days=2555),
        )
        db_session.add(document)
    db_session.commit()

    response = client.get(f"/api/v1/documents/documents?loan_application_id={loan_app.id}")

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 3
    assert len(data["documents"]) == 3


@pytest.mark.asyncio
async def test_delete_document(client, db_session, mock_s3_client):
    # Mock the storage service's S3 client and bucket name
    from app.services.storage import storage_service
    from unittest.mock import MagicMock

    original_s3_client = storage_service._s3_client
    original_bucket = getattr(storage_service, '_bucket_name', None)

    # Create a mock S3 client
    mock_s3 = MagicMock()
    storage_service._s3_client = mock_s3
    storage_service._bucket_name = "test-bucket"

    try:

        # Create test data with proper FK relationships
        vendor = Vendor(
            id=uuid4(),
            business_name="Test Vendor",
            business_type="sole_proprietor",
            contact_name="Test Contact",
            email="vendor@example.com",
            phone="555-1234",
            address_line1="123 Test St",
            city="Test City",
            province="BC",
            postal_code="V1V 1V1",
        )
        db_session.add(vendor)
        db_session.flush()

        borrower = Borrower(
            id=uuid4(),
            first_name="Test",
            last_name="Borrower",
            email="borrower@example.com",
            phone="555-5678",
            date_of_birth=datetime.now(timezone.utc),
            address_line1="123 Borrower St",
            city="Borrower City",
            province="BC",
            postal_code="V1V 1V1",
        )
        db_session.add(borrower)
        db_session.flush()

        loan_app = LoanApplication(
            id=uuid4(),
            borrower_id=borrower.id,
            vendor_id=vendor.id,
            requested_amount=1000.00,
        )
        db_session.add(loan_app)
        db_session.flush()

        document = Document(
            id=uuid4(),
            loan_application_id=loan_app.id,
            document_type="id_front",
            title="Test ID",
            status="uploaded",
            s3_object_key="test.jpg",
            s3_bucket="test-bucket",
            file_name="test.jpg",
            expires_at=datetime.now(timezone.utc) + timedelta(days=2555),
        )
        db_session.add(document)
        db_session.commit()

        response = client.delete(f"/api/v1/documents/documents/{document.id}")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
    finally:
        # Restore original S3 client and bucket name
        storage_service._s3_client = original_s3_client
        if original_bucket:
            storage_service._bucket_name = original_bucket
        elif hasattr(storage_service, '_bucket_name'):
            delattr(storage_service, '_bucket_name')