"""Mock / test-fill helper for the canonical credit application.

Dave wants a test application to carry *realistic* values through the whole
backend (not empty columns), so a demo/QA run exercises the same fields a real
applicant would supply. This module generates realistic RANDOM data matching the
canonical schema (``app/models/platform/credit_application.py``), writes it onto
an application, and routes the application to MANUAL underwriting — exactly like
the manual-fallback path, because mock data has no real verifications to decide
on.

Design:
  * Pure-ish data generation (:func:`generate_mock_application_data`) is DB-free
    and deterministic under a seed, so it can be unit-tested without Postgres.
  * :func:`mock_complete_application` applies that data to an application row +
    its patient, adds secondary-income child rows, and stamps the status via the
    orchestrator's ``mark_manual_review`` (status writes are owned by the
    orchestrator — this module never assigns ``application.status`` itself).

SECURITY: the SIN is never stored in plaintext. A valid random SIN is generated,
encrypted via ``app.core.sin_crypto.encrypt_sin`` onto ``patient.sin_encrypted``,
and only ``sin_last3`` is retained. No raw SIN is written to any JSONB/dict column
or returned.

These helpers are for DEV/TEST/DEMO only (mock mode). They must never run against
real applicant data in production.
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.core.sin_crypto import encrypt_sin
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.patient import PlatformPatient
from app.models.platform.secondary_income import PlatformApplicationSecondaryIncome
from app.services.flow_orchestrator import mark_manual_review

# --- sample pools (realistic Canadian values) ------------------------------

_FIRST_NAMES = ["Alex", "Jordan", "Taylor", "Morgan", "Casey", "Jamie", "Riley",
                "Avery", "Cameron", "Dana", "Priya", "Wei", "Fatima", "Liam"]
_MIDDLE_NAMES = ["Lee", "Ray", "Quinn", "Blake", "Drew", None, None, None]
_LAST_NAMES = ["Smith", "Nguyen", "Patel", "Tremblay", "Wong", "Johnson",
               "Singh", "Martin", "Roy", "Gagnon", "Brown", "Lee"]
_MARITAL = ["single", "married", "common_law", "divorced", "widowed", "separated"]
_CITIZENSHIP = ["canadian_citizen", "permanent_resident", "work_permit"]
_EDUCATION = ["high_school", "college_diploma", "bachelors", "masters",
              "trade_certificate", None]
_PROVINCES = ["BC", "AB", "ON", "MB", "SK", "NS", "NB"]  # QC intentionally omitted
_ID_TYPES = ["drivers_licence", "provincial_id", "passport"]
_RESIDENTIAL_STATUS = ["own", "rent", "living_with_family", "board"]
_INCOME_TYPES = ["employed_full_time", "employed_part_time", "employed_seasonal",
                 "self_employed", "retirement_pension", "disability",
                 "employment_insurance", "other"]
_SECONDARY_INCOME_TYPES = ["employed_part_time", "self_employed",
                           "retirement_pension", "disability",
                           "employment_insurance", "other"]
_PAY_FREQ = ["weekly", "biweekly", "semimonthly", "monthly"]
_CAR_OWNERSHIP = ["fully_paid", "financing", "leasing", "none"]
_EMPLOYERS = ["Maple Retail Ltd", "Northern Logistics", "Coast Health Services",
              "Prairie Foods Inc", "Cedar Tech", "Harbourview Dental",
              "Summit Construction", "Riverside Cafe"]
_JOB_TITLES = ["Administrator", "Technician", "Sales Associate", "Nurse",
               "Driver", "Manager", "Analyst", "Server", "Electrician"]
_CITIES = ["Kelowna", "Vancouver", "Calgary", "Toronto", "Winnipeg",
           "Halifax", "Saskatoon"]


def _luhn_check_digit_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(digits):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _random_valid_sin(rng: random.Random) -> str:
    """Return a 9-digit string that passes the Canadian SIN Luhn check.

    Generated purely for mock/demo data — not a real person's SIN. Avoids a
    leading 0 (invalid first digit) and brute-forces a Luhn-valid 9-digit string.
    """
    while True:
        first = rng.randint(1, 9)
        rest = [rng.randint(0, 9) for _ in range(8)]
        digits = str(first) + "".join(str(d) for d in rest)
        if _luhn_check_digit_ok(digits):
            return digits


def _random_phone(rng: random.Random) -> str:
    return f"+1{rng.randint(200, 999)}{rng.randint(200, 999)}{rng.randint(1000, 9999)}"


def _random_postal(rng: random.Random) -> str:
    letters = "ABCEGHJKLMNPRSTVXY"
    return (
        f"{rng.choice(letters)}{rng.randint(0,9)}{rng.choice(letters)} "
        f"{rng.randint(0,9)}{rng.choice(letters)}{rng.randint(0,9)}"
    )


def generate_mock_application_data(
    seed: Optional[int] = None,
) -> dict[str, Any]:
    """Generate a realistic random canonical-application payload (DB-free).

    Returns a dict with:
      * ``application``: kwargs for the canonical columns on
        ``PlatformCreditApplication`` (dates as ``date`` objects, money as cents).
      * ``secondary_incomes``: a list (0-2) of secondary-income kwargs.
      * ``raw_sin``: the raw 9-digit SIN (Luhn-valid). SECURITY: the caller MUST
        route this through ``encrypt_sin`` and retain only ``sin_last3`` — never
        persist it in plaintext. (Keyed ``raw_sin`` rather than ``sin`` because it
        is an in-memory generator value, never a persisted JSONB field.)
      * ``sin_last3``: the masked last three digits.

    Deterministic under ``seed`` so tests can assert on exact values.
    """
    rng = random.Random(seed)
    today = date.today()

    income_type = rng.choice(_INCOME_TYPES)
    car_ownership = rng.choice(_CAR_OWNERSHIP)
    residential_status = rng.choice(_RESIDENTIAL_STATUS)
    sin = _random_valid_sin(rng)

    # A working-age DOB: 19-70 years old.
    age_days = rng.randint(19 * 365, 70 * 365)
    dob = today - timedelta(days=age_days)

    net_monthly = rng.randint(180000, 850000)  # $1,800 - $8,500 in cents

    application = {
        # Personal
        "first_name": rng.choice(_FIRST_NAMES),
        "middle_name": rng.choice(_MIDDLE_NAMES),
        "last_name": rng.choice(_LAST_NAMES),
        "date_of_birth": dob,
        "marital_status": rng.choice(_MARITAL),
        "number_of_dependents": rng.randint(0, 4),
        "citizenship": rng.choice(_CITIZENSHIP),
        "education": rng.choice(_EDUCATION),
        "main_phone": _random_phone(rng),
        "alternative_phone": _random_phone(rng) if rng.random() < 0.4 else None,
        "email": f"mock.applicant+{rng.randint(1000, 9999)}@example.com",
        # ID verification
        "id_type": rng.choice(_ID_TYPES),
        "id_number": f"{rng.choice('ABCDEFGH')}{rng.randint(100000, 999999)}",
        "id_province_of_issue": rng.choice(_PROVINCES),
        "id_expiry": today + timedelta(days=rng.randint(200, 2500)),
        # Residence
        "residence_street": f"{rng.randint(10, 9999)} {rng.choice(['Maple','Oak','Cedar','Lake','Main','Elm'])} St",
        "residence_unit": str(rng.randint(1, 40)) if rng.random() < 0.35 else None,
        "residence_city": rng.choice(_CITIES),
        "residence_province": rng.choice(_PROVINCES),
        "residence_postal_code": _random_postal(rng),
        "time_at_address_years": rng.randint(0, 15),
        "time_at_address_months": rng.randint(0, 11),
        "residential_status": residential_status,
        "monthly_housing_payment_cents": (
            0 if residential_status == "living_with_family" else rng.randint(80000, 320000)
        ),
        # Primary income
        "income_type": income_type,
        "net_monthly_income_cents": net_monthly,
        "next_pay_date": today + timedelta(days=rng.randint(1, 28)),
        "pay_frequency": rng.choice(_PAY_FREQ),
        "employer_name": rng.choice(_EMPLOYERS),
        "hire_date": today - timedelta(days=rng.randint(90, 4000)),
        "job_title": rng.choice(_JOB_TITLES),
        "work_phone": _random_phone(rng),
        "work_phone_ext": str(rng.randint(100, 999)) if rng.random() < 0.3 else None,
        "ok_to_contact_at_work": rng.random() < 0.5,
        # Financial
        "number_of_credit_accounts": rng.randint(0, 12),
        "car_ownership": car_ownership,
        "monthly_car_payment_cents": (
            rng.randint(20000, 80000) if car_ownership in ("financing", "leasing") else 0
        ),
        "non_discretionary_expenses_cents": rng.randint(50000, 400000),
    }

    secondary_incomes: list[dict[str, Any]] = []
    for _ in range(rng.randint(0, 2)):
        secondary_incomes.append(
            {
                "income_type": rng.choice(_SECONDARY_INCOME_TYPES),
                "net_monthly_income_cents": rng.randint(20000, 300000),
                "pay_frequency": rng.choice(_PAY_FREQ),
                "next_pay_date": today + timedelta(days=rng.randint(1, 28)),
                "employer_name": rng.choice(_EMPLOYERS),
                "job_title": rng.choice(_JOB_TITLES),
                "hire_date": today - timedelta(days=rng.randint(90, 3000)),
                "work_phone": _random_phone(rng),
                "work_phone_ext": None,
                "description": rng.choice(
                    ["side gig", "rental income", "pension", "part-time shift", None]
                ),
            }
        )

    # NB: the raw SIN is returned under ``raw_sin`` (not ``sin``) deliberately —
    # this is an in-memory generator payload that is NEVER persisted to a JSONB
    # column; only ``sin_last3`` and the encrypted token are stored downstream.
    return {
        "application": application,
        "secondary_incomes": secondary_incomes,
        "raw_sin": sin,
        "sin_last3": sin[-3:],
    }


def mock_complete_application(
    db: Session,
    application: PlatformCreditApplication,
    *,
    seed: Optional[int] = None,
    route_to_manual_review: bool = True,
    commit: bool = True,
) -> PlatformCreditApplication:
    """Populate ``application`` with realistic random canonical data + route it.

    Writes the canonical columns, creates 0-2 secondary-income child rows, and
    (unless disabled) moves the application to MANUAL underwriting via
    ``mark_manual_review`` (status writes stay owned by the orchestrator).

    The SIN is generated valid, encrypted onto the patient via ``encrypt_sin`` and
    reduced to ``sin_last3`` — it is NEVER stored in plaintext.

    Idempotency: replaces any existing secondary-income rows so a re-run leaves a
    clean set rather than accumulating duplicates.
    """
    data = generate_mock_application_data(seed=seed)
    now = datetime.now(timezone.utc)

    for key, value in data["application"].items():
        setattr(application, key, value)

    # SIN → encrypted on the patient (never plaintext on the application).
    patient = (
        db.query(PlatformPatient)
        .filter(PlatformPatient.id == application.patient_id)
        .first()
    )
    if patient is not None:
        raw_sin = data["raw_sin"]
        patient.sin_encrypted = encrypt_sin(raw_sin)
        patient.sin_last3 = data["sin_last3"]
        patient.sin_collected_at = now
        patient.sin_declined = False
        patient.sin_declined_at = None
        db.add(patient)

    # Replace secondary incomes (idempotent re-run).
    db.query(PlatformApplicationSecondaryIncome).filter(
        PlatformApplicationSecondaryIncome.application_id == application.id
    ).delete(synchronize_session=False)
    for line in data["secondary_incomes"]:
        db.add(
            PlatformApplicationSecondaryIncome(application_id=application.id, **line)
        )

    # Mark this as mock-filled in flow_state so downstream code + reviewers can tell.
    flow_state = dict(application.flow_state or {})
    flow_state["mock_filled"] = True
    flow_state["mock_filled_at"] = now.isoformat()
    application.flow_state = flow_state

    if route_to_manual_review and application.status not in _TERMINAL:
        # Status transition owned by the orchestrator module.
        mark_manual_review(application)
        application.status_updated_at = now

    db.add(application)
    if commit:
        db.commit()
        db.refresh(application)
    return application


# Terminal states a mock-complete must not reopen (mirrors the orchestrator).
_TERMINAL = ("approved", "declined", "withdrawn", "expired")
