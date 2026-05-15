import pytest
from unittest.mock import Mock, patch
from uuid import uuid4
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from app.models.document import Document
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


def test_storage_service_generate_presigned_upload_url(mock_s3_client):
    mock_s3_client.generate_presigned_post.return_value = {
        "url": "https://s3.amazonaws.com/bucket",
        "fields": {"key": "value"},
    }

    service = StorageService()
    service._bucket_name = "test-bucket"

    result = service.generate_presigned_upload_url(
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


def test_storage_service_generate_presigned_download_url(mock_s3_client):
    mock_s3_client.generate_presigned_url.return_value = "https://s3.amazonaws.com/bucket/key?signature=xxx"

    service = StorageService()
    service._bucket_name = "test-bucket"

    url = service.generate_presigned_download_url("kyc_sessions/123/id_front/test.jpg")

    assert url.startswith("https://")


def test_storage_service_get_object_metadata(mock_s3_client):
    mock_s3_client.head_object.return_value = {
        "ContentType": "image/jpeg",
        "ContentLength": 1024,
        "LastModified": datetime.now(timezone.utc),
        "Metadata": {"custom": "value"},
        "ETag": '"abc123"',
    }

    service = StorageService()
    service._bucket_name = "test-bucket"

    metadata = service.get_object_metadata("kyc_sessions/123/id_front/test.jpg")

    assert metadata["content_type"] == "image/jpeg"
    assert metadata["content_length"] == 1024
    assert metadata["etag"] == '"abc123"'


def test_storage_service_verify_object_exists(mock_s3_client):
    mock_s3_client.head_object.return_value = {}

    service = StorageService()
    service._bucket_name = "test-bucket"

    exists = service.verify_object_exists("kyc_sessions/123/id_front/test.jpg")

    assert exists is True


def test_storage_service_verify_object_not_found(mock_s3_client):
    mock_s3_client.head_object.side_effect = Exception("404")

    service = StorageService()
    service._bucket_name = "test-bucket"

    with pytest.raises(HTTPException) as exc:
        service.verify_object_exists("nonexistent.jpg")

    assert exc.value.status_code == 500


@pytest.mark.asyncio
async def test_initiate_document_upload(client, db):
    with patch("app.services.storage.StorageService") as mock_service:
        mock_storage = Mock()
        mock_service.return_value = mock_storage
        mock_storage._bucket_name = "test-bucket"
        mock_storage.generate_presigned_upload_url.return_value = {
            "url": "https://s3.amazonaws.com/test",
            "fields": {"key": "value"},
            "object_key": "kyc_sessions/123/id_front/test.jpg",
            "expires_in": 3600,
        }

        request_data = DocumentUploadRequest(
            document_type="id_front",
            title="Test ID",
            file_name="test.jpg",
            file_content_type="image/jpeg",
            loan_application_id=uuid4(),
        )

        response = client.post("/api/v1/documents/upload/initiate", json=request_data.dict())

        assert response.status_code == 200
        data = response.json()
        assert "upload_url" in data
        assert "document_id" in data


@pytest.mark.asyncio
async def test_confirm_document_upload(client, db, mock_s3_client):
    with patch("app.services.storage.StorageService") as mock_service:
        mock_storage = Mock()
        mock_service.return_value = mock_storage
        mock_storage._bucket_name = "test-bucket"
        mock_storage.verify_object_exists.return_value = True
        mock_storage.get_object_metadata.return_value = {
            "ContentType": "image/jpeg",
            "ContentLength": 1024,
            "ETag": '"abc123"',
        }

        document = Document(
            id=uuid4(),
            loan_application_id=uuid4(),
            document_type="id_front",
            title="Test ID",
            status="uploading",
            s3_object_key="test.jpg",
            s3_bucket="test-bucket",
            file_name="test.jpg",
            expires_at=datetime.now(timezone.utc) + timedelta(days=2555),
        )
        db.add(document)
        db.commit()

        response = client.post(
            "/api/v1/documents/upload/confirm",
            json={"document_id": str(document.id), "file_size_bytes": 1024},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "uploaded"
        assert data["file_size_bytes"] == 1024


@pytest.mark.asyncio
async def test_get_document(client, db):
    document = Document(
        id=uuid4(),
        loan_application_id=uuid4(),
        document_type="id_front",
        title="Test ID",
        status="uploaded",
        s3_object_key="test.jpg",
        s3_bucket="test-bucket",
        file_name="test.jpg",
        expires_at=datetime.now(timezone.utc) + timedelta(days=2555),
    )
    db.add(document)
    db.commit()

    response = client.get(f"/api/v1/documents/{document.id}")

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(document.id)
    assert data["title"] == "Test ID"


@pytest.mark.asyncio
async def test_list_documents(client, db):
    loan_app_id = uuid4()

    for i in range(3):
        document = Document(
            id=uuid4(),
            loan_application_id=loan_app_id,
            document_type="id_front",
            title=f"Test ID {i}",
            status="uploaded",
            s3_object_key=f"test{i}.jpg",
            s3_bucket="test-bucket",
            file_name=f"test{i}.jpg",
            expires_at=datetime.now(timezone.utc) + timedelta(days=2555),
        )
        db.add(document)
    db.commit()

    response = client.get(f"/api/v1/documents?loan_application_id={loan_app_id}")

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 3
    assert len(data["documents"]) == 3


@pytest.mark.asyncio
async def test_delete_document(client, db, mock_s3_client):
    with patch("app.services.storage.StorageService") as mock_service:
        mock_storage = Mock()
        mock_service.return_value = mock_storage
        mock_storage._bucket_name = "test-bucket"
        mock_storage.delete_object.return_value = True

        document = Document(
            id=uuid4(),
            loan_application_id=uuid4(),
            document_type="id_front",
            title="Test ID",
            status="uploaded",
            s3_object_key="test.jpg",
            s3_bucket="test-bucket",
            file_name="test.jpg",
            expires_at=datetime.now(timezone.utc) + timedelta(days=2555),
        )
        db.add(document)
        db.commit()

        response = client.delete(f"/api/v1/documents/{document.id}")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True