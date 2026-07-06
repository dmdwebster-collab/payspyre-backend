"""Staff/admin access tokens get the workday lifetime; others stay short (DB-free).

Task 1: the lender/admin cockpit is an all-day tool, so staff/admin logins mint an
access token with ``JWT_STAFF_ACCESS_TOKEN_EXPIRE_MINUTES`` (~10h). Applicant /
borrower / role-less sessions keep the short ``JWT_ACCESS_TOKEN_EXPIRE_MINUTES``.
These are pure token-lifetime checks — no database.
"""
from datetime import datetime, timedelta

from app.core.config import settings
from app.core.security import (
    STAFF_SESSION_ROLES,
    create_staff_access_token,
    decode_token,
)


def _lifetime_minutes(token: str) -> float:
    payload = decode_token(token)
    assert payload is not None and payload["type"] == "access"
    exp = datetime.utcfromtimestamp(payload["exp"])
    return (exp - datetime.utcnow()).total_seconds() / 60.0


def _approx(actual: float, expected_minutes: int) -> bool:
    # Allow a minute of slack for clock drift between mint and assertion.
    return abs(actual - expected_minutes) <= 1.0


def test_staff_role_gets_workday_lifetime():
    token = create_staff_access_token({"sub": "u"}, roles=["staff"])
    assert _approx(_lifetime_minutes(token), settings.JWT_STAFF_ACCESS_TOKEN_EXPIRE_MINUTES)


def test_admin_role_gets_workday_lifetime():
    token = create_staff_access_token({"sub": "u"}, roles=["admin"])
    assert _approx(_lifetime_minutes(token), settings.JWT_STAFF_ACCESS_TOKEN_EXPIRE_MINUTES)


def test_mixed_roles_including_staff_get_workday_lifetime():
    token = create_staff_access_token({"sub": "u"}, roles=["patient", "admin"])
    assert _approx(_lifetime_minutes(token), settings.JWT_STAFF_ACCESS_TOKEN_EXPIRE_MINUTES)


def test_non_staff_role_stays_short():
    token = create_staff_access_token({"sub": "u"}, roles=["patient"])
    assert _approx(_lifetime_minutes(token), settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)


def test_no_roles_stays_short():
    token = create_staff_access_token({"sub": "u"}, roles=[])
    assert _approx(_lifetime_minutes(token), settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)


def test_staff_lifetime_is_longer_than_applicant():
    # The whole point: the workday session outlives the short consumer session.
    assert (
        settings.JWT_STAFF_ACCESS_TOKEN_EXPIRE_MINUTES
        > settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES
    )
    # ~10 hours (a full workday), give or take.
    assert settings.JWT_STAFF_ACCESS_TOKEN_EXPIRE_MINUTES >= 8 * 60


def test_staff_session_roles_are_admin_and_staff():
    assert STAFF_SESSION_ROLES == frozenset({"admin", "staff"})
