"""KYC document upload endpoints + storage service (manual-fallback path).

Storage is INERT until SPACES_* is set: the service reports unconfigured and the
upload-url endpoint returns 503. With test credentials, boto3 generates presigned
URLs offline (no network), so the happy path is exercised without real Spaces.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.applicant.v1.deps import get_notification_dispatcher
from app.api.applicant.v1.endpoints import documents as documents_module
from app.core.config import settings
from app.main import app
from app.models.platform.application_document import PlatformApplicationDocument
from app.models.platform.credit_product import PlatformCreditProduct
from app.services.mock_notification_dispatcher import MockNotificationDispatcher
from app.services import document_storage

_BASE = "/api/applicant/v1"

_DOC_PATH = f"{_BASE}/applications/{{application_id}}/documents/upload-url"
if not any(getattr(r, "path", None) == _DOC_PATH for r in app.router.routes):
    app.include_router(documents_module.router, prefix=_BASE)


@pytest.fixture
def dispatcher(db_session: Session):
    disp = MockNotificationDispatcher(db_session)
    app.dependency_overrides[get_notification_dispatcher] = lambda: disp
    yield disp
    app.dependency_overrides.pop(get_notification_dispatcher, None)


@pytest.fixture
def configured(monkeypatch):
    """Set fake Spaces creds so the service reports configured (boto3 signs offline)."""
    monkeypatch.setattr(settings, "SPACES_BUCKET", "test-bucket")
    monkeypatch.setattr(settings, "SPACES_KEY", "test-key")
    monkeypatch.setattr(settings, "SPACES_SECRET", "test-secret")
    monkeypatch.setattr(settings, "SPACES_REGION", "tor1")
    monkeypatch.setattr(settings, "SPACES_ENDPOINT", "")


def _product_id(db: Session) -> uuid.UUID:
    return (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.code == "dental_full_arch_v1")
        .first()
        .id
    )


def _auth(client: TestClient, db: Session, dispatcher) -> tuple[str, dict]:
    resp = client.post(
        f"{_BASE}/applications",
        json={
            "patient_profile": {"legal_first_name": "Jo", "email": f"doc-{uuid.uuid4().hex[:8]}@example.com"},
            "credit_product_id": str(_product_id(db)),
            "requested_amount_cents": 3_000_000,
            "requested_amount_source": "clinic",
            "contact_method": "email",
        },
    )
    assert resp.status_code == 201, resp.text
    app_id = resp.json()["application_id"]
    token = dispatcher._sent[-1]["token"]
    ex = client.post(f"{_BASE}/auth/magic-link/exchange", json={"application_id": app_id, "token": token})
    assert ex.status_code == 200, ex.text
    return app_id, {"Authorization": f"Bearer {ex.json()['jwt']}"}


class TestStorageService:
    def test_unconfigured_by_default(self, monkeypatch):
        monkeypatch.setattr(settings, "SPACES_BUCKET", "")
        assert document_storage.is_configured() is False
        with pytest.raises(document_storage.StorageNotConfigured):
            document_storage.presigned_put_url("k", "image/jpeg")

    def test_presigned_urls_generated_when_configured(self, configured):
        assert document_storage.is_configured() is True
        url = document_storage.presigned_put_url("applications/a/id_front/x", "image/jpeg")
        assert url.startswith("https://test-bucket.tor1.digitaloceanspaces.com/") or "test-bucket" in url
        assert "X-Amz-Signature" in url


class TestDocumentEndpoints:
    def test_upload_url_503_when_storage_unconfigured(self, client, db_session, dispatcher, monkeypatch):
        monkeypatch.setattr(settings, "SPACES_BUCKET", "")  # inert
        app_id, headers = _auth(client, db_session, dispatcher)
        resp = client.post(
            f"{_BASE}/applications/{app_id}/documents/upload-url",
            json={"doc_type": "id_front", "content_type": "image/jpeg"}, headers=headers,
        )
        assert resp.status_code == 503, resp.text

    def test_upload_url_then_confirm_and_list(self, client, db_session, dispatcher, configured):
        app_id, headers = _auth(client, db_session, dispatcher)
        resp = client.post(
            f"{_BASE}/applications/{app_id}/documents/upload-url",
            json={"doc_type": "id_front", "content_type": "image/jpeg"}, headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["upload_url"].startswith("https://")
        doc_id = body["document_id"]

        # a pending row was persisted
        doc = db_session.query(PlatformApplicationDocument).filter_by(id=doc_id).first()
        assert doc is not None and doc.status == "pending"

        conf = client.post(
            f"{_BASE}/applications/{app_id}/documents/{doc_id}/confirm", headers=headers,
        )
        assert conf.status_code == 200 and conf.json()["status"] == "uploaded"

        lst = client.get(f"{_BASE}/applications/{app_id}/documents", headers=headers)
        assert lst.status_code == 200
        assert any(d["document_id"] == doc_id and d["status"] == "uploaded" for d in lst.json())

    def test_rejects_bad_doc_type(self, client, db_session, dispatcher, configured):
        app_id, headers = _auth(client, db_session, dispatcher)
        resp = client.post(
            f"{_BASE}/applications/{app_id}/documents/upload-url",
            json={"doc_type": "passport_selfie", "content_type": "image/jpeg"}, headers=headers,
        )
        assert resp.status_code == 422


def test_id_document_upload_consent_registered():
    from app.services import consent_service
    ct = consent_service.get_active_consent_text("id_document_upload")
    assert ct.version == "v1_2026-06"
