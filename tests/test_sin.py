"""SIN collection + encryption-at-rest (audit C-3).

NO live DB, NO full suite. The crypto / validation tests are pure unit tests.
The endpoint tests drive the real FastAPI app via TestClient but replace the DB
with an in-memory fake session and the auth dependency with synthetic claims, so
nothing here touches Postgres.

The security-critical assertions: the full SIN is NEVER in any response body and
NEVER in captured logs — only ``sin_last3`` is ever surfaced.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet, InvalidToken
from fastapi.testclient import TestClient

from app.core import sin_crypto
from app.core.sin_crypto import decrypt_sin, encrypt_sin, get_sin_for_bureau
from app.core.validation import normalize_sin


# A real Canadian SIN that passes the Luhn check (commonly used test SIN).
VALID_SIN = "046454286"
VALID_LAST3 = "286"


# --------------------------------------------------------------------------
# fixtures: toggle the dedicated SIN key without mutating pydantic Settings
# --------------------------------------------------------------------------


@pytest.fixture
def sin_key(monkeypatch):
    key = Fernet.generate_key().decode("utf-8")
    monkeypatch.setattr(sin_crypto, "_configured_key", lambda: key)
    monkeypatch.setattr(sin_crypto, "_warned_no_key", False, raising=False)
    return key


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.setattr(sin_crypto, "_configured_key", lambda: "")
    monkeypatch.setattr(sin_crypto, "_warned_no_key", False, raising=False)


# --------------------------------------------------------------------------
# crypto round-trip
# --------------------------------------------------------------------------


def test_encrypt_decrypt_round_trip(sin_key):
    stored = encrypt_sin(VALID_SIN)
    assert stored is not None
    assert stored.startswith(sin_crypto._MARKER)
    assert VALID_SIN not in stored  # ciphertext does not leak the plaintext
    assert decrypt_sin(stored) == VALID_SIN


def test_ciphertext_is_nondeterministic(sin_key):
    # Fernet uses a random IV — same SIN, different tokens, both decrypt back.
    a = encrypt_sin(VALID_SIN)
    b = encrypt_sin(VALID_SIN)
    assert a != b
    assert decrypt_sin(a) == decrypt_sin(b) == VALID_SIN


def test_no_key_is_passthrough(no_key):
    # Dev no-op: stored unchanged, reads back unchanged.
    assert encrypt_sin(VALID_SIN) == VALID_SIN
    assert decrypt_sin(VALID_SIN) == VALID_SIN


def test_encrypt_none_and_empty(sin_key):
    assert encrypt_sin(None) is None
    assert encrypt_sin("") is None
    assert decrypt_sin(None) is None


def test_encrypt_is_idempotent(sin_key):
    once = encrypt_sin(VALID_SIN)
    twice = encrypt_sin(once)  # already-marked: not double-wrapped
    assert once == twice
    assert decrypt_sin(twice) == VALID_SIN


def test_decrypt_with_wrong_key_raises(sin_key, monkeypatch):
    stored = encrypt_sin(VALID_SIN)
    # Rotate to a different key, then decrypt should hard-fail.
    other = Fernet.generate_key().decode("utf-8")
    monkeypatch.setattr(sin_crypto, "_configured_key", lambda: other)
    with pytest.raises(InvalidToken):
        decrypt_sin(stored)


def test_decrypt_marked_value_without_key_raises(sin_key, monkeypatch):
    stored = encrypt_sin(VALID_SIN)
    monkeypatch.setattr(sin_crypto, "_configured_key", lambda: "")
    with pytest.raises(InvalidToken):
        decrypt_sin(stored)


def test_uses_dedicated_sin_key_not_settings_key():
    # The SIN module reads SIN_ENCRYPTION_KEY, separate from secret_crypto.
    assert sin_crypto._MARKER == "sinv1:"
    from app.core import secret_crypto

    assert sin_crypto._MARKER != secret_crypto._MARKER


# --------------------------------------------------------------------------
# validation: normalize_sin rejects bad SINs, accepts valid ones
# --------------------------------------------------------------------------


def test_normalize_accepts_valid_sin():
    assert normalize_sin(VALID_SIN) == VALID_SIN
    # Accepts dashed/spaced formatting.
    assert normalize_sin("046 454 286") == VALID_SIN


@pytest.mark.parametrize(
    "bad",
    [
        "12345678",       # too short
        "1234567890",     # too long
        "046454287",      # fails Luhn (last digit off by one)
        "123456789",      # 9 digits but fails Luhn
        "abcdefghi",      # non-numeric
    ],
)
def test_normalize_rejects_bad_sins(bad):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        normalize_sin(bad)
    assert exc.value.status_code == 422


# --------------------------------------------------------------------------
# get_sin_for_bureau
# --------------------------------------------------------------------------


class _FakePatient:
    def __init__(self, sin_encrypted=None):
        self.sin_encrypted = sin_encrypted


def test_get_sin_for_bureau_round_trip(sin_key):
    p = _FakePatient(sin_encrypted=encrypt_sin(VALID_SIN))
    assert get_sin_for_bureau(p) == VALID_SIN


def test_get_sin_for_bureau_none_when_absent(sin_key):
    assert get_sin_for_bureau(_FakePatient(sin_encrypted=None)) is None


# --------------------------------------------------------------------------
# endpoint: in-memory fake DB, synthetic auth — no Postgres
# --------------------------------------------------------------------------


class _FakeQuery:
    def __init__(self, result):
        self._result = result

    def filter(self, *_, **__):
        return self

    def first(self):
        return self._result


class _PatientRow:
    """Stand-in for PlatformPatient with just the SIN columns the endpoint writes."""

    def __init__(self, pid):
        self.id = pid
        self.sin_encrypted = None
        self.sin_last3 = None
        self.sin_collected_at = None
        self.sin_declined = False
        self.sin_declined_at = None


class _AppRow:
    def __init__(self, app_id, patient_id):
        self.id = app_id
        self.patient_id = patient_id


class _FakeSession:
    """Routes .query(Model) to the right fixed row; records commits."""

    def __init__(self, application, patient):
        self._application = application
        self._patient = patient
        self.committed = False

    def query(self, model):
        from app.models.platform.credit_application import PlatformCreditApplication
        from app.models.platform.patient import PlatformPatient

        if model is PlatformCreditApplication:
            return _FakeQuery(self._application)
        if model is PlatformPatient:
            return _FakeQuery(self._patient)
        return _FakeQuery(None)

    def add(self, _obj):
        pass

    def commit(self):
        self.committed = True


@pytest.fixture
def sin_client(sin_key):
    """TestClient with DB + auth dependencies overridden (no Postgres, no JWT)."""
    from app.api.applicant.v1.deps import ApplicantClaims, get_current_applicant
    from app.db.base import get_db
    from app.main import app

    app_id = uuid.uuid4()
    patient_id = uuid.uuid4()
    patient = _PatientRow(patient_id)
    application = _AppRow(app_id, patient_id)
    session = _FakeSession(application, patient)

    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_applicant] = lambda: ApplicantClaims(
        patient_id=patient_id, app_ids=[app_id]
    )
    try:
        yield TestClient(app), str(app_id), patient, session
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_applicant, None)


_SIN_URL = "/api/applicant/v1/applications/{app_id}/sin"


def test_collect_sin_stores_last3_and_never_echoes(sin_client):
    client, app_id, patient, session = sin_client
    resp = client.post(_SIN_URL.format(app_id=app_id), json={"sin": VALID_SIN})
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Response carries ONLY last3 + flags.
    assert body == {"sin_last3": VALID_LAST3, "collected": True, "declined": False}
    # The full SIN is nowhere in the response body.
    assert VALID_SIN not in resp.text

    # Stored encrypted (marked token), last3 set, committed.
    assert patient.sin_last3 == VALID_LAST3
    assert patient.sin_encrypted is not None
    assert patient.sin_encrypted.startswith(sin_crypto._MARKER)
    assert VALID_SIN not in patient.sin_encrypted
    assert patient.sin_collected_at is not None
    assert session.committed is True
    # Decrypts back to the original — proves it's the real SIN under encryption.
    assert decrypt_sin(patient.sin_encrypted) == VALID_SIN


def test_collect_sin_rejects_invalid(sin_client):
    client, app_id, patient, _ = sin_client
    resp = client.post(_SIN_URL.format(app_id=app_id), json={"sin": "046454287"})
    assert resp.status_code == 422, resp.text
    assert patient.sin_encrypted is None  # nothing stored on rejection


def test_decline_path(sin_client):
    client, app_id, patient, session = sin_client
    resp = client.post(_SIN_URL.format(app_id=app_id), json={"declined": True})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"sin_last3": None, "collected": False, "declined": True}
    assert patient.sin_declined is True
    assert patient.sin_declined_at is not None
    assert patient.sin_encrypted is None


def test_body_requires_exactly_one_intent(sin_client):
    client, app_id, _, _ = sin_client
    # Neither sin nor declined -> 422 (pydantic validation error).
    resp = client.post(_SIN_URL.format(app_id=app_id), json={})
    assert resp.status_code == 422
    # Both -> 422.
    resp2 = client.post(
        _SIN_URL.format(app_id=app_id), json={"sin": VALID_SIN, "declined": True}
    )
    assert resp2.status_code == 422


def test_recollection_overwrites_and_clears_decline(sin_client):
    client, app_id, patient, _ = sin_client
    # Decline first.
    client.post(_SIN_URL.format(app_id=app_id), json={"declined": True})
    assert patient.sin_declined is True
    # Then collect — overwrites, clears the decline.
    resp = client.post(_SIN_URL.format(app_id=app_id), json={"sin": VALID_SIN})
    assert resp.status_code == 200
    assert patient.sin_declined is False
    assert patient.sin_declined_at is None
    assert patient.sin_last3 == VALID_LAST3


def test_sin_never_appears_in_logs(sin_client, caplog):
    client, app_id, _, _ = sin_client
    with caplog.at_level(logging.DEBUG):
        resp = client.post(_SIN_URL.format(app_id=app_id), json={"sin": VALID_SIN})
    assert resp.status_code == 200
    # The raw SIN must not appear anywhere in captured log output.
    for record in caplog.records:
        assert VALID_SIN not in record.getMessage()
    assert VALID_SIN not in caplog.text
