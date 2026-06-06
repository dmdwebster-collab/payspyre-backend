"""Tests for the multi-key rate limiter's user-id resolution.

Pure (no DB, no FastAPI app). Security review #5: per-account rate limiting must
work for BOTH staff tokens (signed with JWT_SECRET_KEY) and patient/applicant
tokens (signed with PATIENT_JWT_SECRET). Decoding patient tokens with the wrong
secret silently returned None, so all authenticated patient traffic fell back to
per-IP limiting (the per-account throttle was dead).
"""
from types import SimpleNamespace

from jose import jwt

from app.core.config import settings
from app.core.rate_limit import limiter


def _req(token: str | None):
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return SimpleNamespace(headers=headers)


class TestGetUserIdBothSecrets:
    def test_patient_token_resolves_user_id(self, monkeypatch):
        monkeypatch.setattr(settings, "JWT_SECRET_KEY", "staff-secret")
        monkeypatch.setattr(settings, "PATIENT_JWT_SECRET", "patient-secret")
        token = jwt.encode(
            {"sub": "patient-123"}, "patient-secret", algorithm=settings.JWT_ALGORITHM
        )
        assert limiter._get_user_id(_req(token)) == "patient-123"

    def test_staff_token_still_resolves(self, monkeypatch):
        monkeypatch.setattr(settings, "JWT_SECRET_KEY", "staff-secret")
        monkeypatch.setattr(settings, "PATIENT_JWT_SECRET", "patient-secret")
        token = jwt.encode(
            {"sub": "staff-1"}, "staff-secret", algorithm=settings.JWT_ALGORITHM
        )
        assert limiter._get_user_id(_req(token)) == "staff-1"

    def test_token_signed_with_neither_secret_returns_none(self, monkeypatch):
        monkeypatch.setattr(settings, "JWT_SECRET_KEY", "staff-secret")
        monkeypatch.setattr(settings, "PATIENT_JWT_SECRET", "patient-secret")
        forged = jwt.encode({"sub": "x"}, "some-other-secret", algorithm=settings.JWT_ALGORITHM)
        assert limiter._get_user_id(_req(forged)) is None

    def test_garbage_token_returns_none(self):
        assert limiter._get_user_id(_req("not-a-jwt")) is None

    def test_no_auth_header_returns_none(self):
        assert limiter._get_user_id(_req(None)) is None
