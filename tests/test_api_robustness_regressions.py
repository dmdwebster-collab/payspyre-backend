"""Deterministic regressions for the 5xx bugs Schemathesis (L3) surfaced.

The property fuzz (test_api_fuzz_schemathesis.py) found these; these tests pin each
fix with a concrete, fast, seed-independent assertion so a regression is caught on
every push regardless of the fuzzer's seed.
"""
from __future__ import annotations

import uuid

from sqlalchemy import text

from app.db.rls import set_rls_context


# --- DB health endpoint (multiple bugs in one path) ------------------------


def test_db_health_endpoint_returns_200(client):
    """GET /api/v1/db used to 500 four different ways: pool.max_overflow (no such
    attr), `SET LOCAL .. = NULL` (invalid SQL → aborted txn), a pg_stat_user_indexes
    query selecting tablename/indexname (wrong columns), and .get() on the bool
    connection check. All fixed — the endpoint must answer 200 with a status."""
    resp = client.get("/api/v1/db")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["overall_status"] in ("healthy", "degraded")


def test_set_rls_context_clear_path_is_valid_sql(db_session):
    """set_rls_context()'s clear block emitted `SET LOCAL app.vendor_id = NULL`,
    a Postgres syntax error that aborts the transaction. It must now run clean and
    leave the GUCs readable as the empty string (which the policies treat as NULL)."""
    set_rls_context(db_session, user_id=str(uuid.uuid4()), user_role="patient")
    # Transaction is still usable (not aborted) and the cleared GUC reads back as ''.
    val = db_session.execute(text("SELECT current_setting('app.vendor_id', true)")).scalar()
    assert val in ("", None)


# --- POST /applications robustness -----------------------------------------


def _product_id(client) -> str:
    products = client.get("/api/applicant/v1/products").json()["products"]
    return products[0]["id"]


def test_create_application_unknown_product_returns_422_not_500(client):
    """A bogus credit_product_id raised a bare OrchestratorError that propagated as
    500. It must now map to a clean 4xx."""
    resp = client.post(
        "/api/applicant/v1/applications",
        json={
            "patient_profile": {"legal_first_name": "Reg", "email": "reg-prod@example.com"},
            "credit_product_id": str(uuid.uuid4()),
            "requested_amount_cents": 3_000_000,
            "requested_amount_source": "patient",
            "contact_method": "email",
        },
    )
    assert resp.status_code == 422, resp.text


def test_create_application_blank_email_does_not_500(client):
    """Empty-string email is falsy, so find-by-email was skipped and a second blank
    applicant collided on the lower(email) unique index → 500. Two blank-email
    applications must now both succeed (email normalized to NULL, no collision)."""
    pid = _product_id(client)
    payload = {
        "patient_profile": {"legal_first_name": "Blank", "email": ""},
        "credit_product_id": pid,
        "requested_amount_cents": 3_000_000,
        "requested_amount_source": "patient",
        "contact_method": "email",
    }
    r1 = client.post("/api/applicant/v1/applications", json=payload)
    r2 = client.post("/api/applicant/v1/applications", json=payload)
    assert r1.status_code == 201, r1.text
    assert r2.status_code == 201, r2.text


def test_create_application_case_insensitive_email_reuses_patient(client):
    """The find-by-email used exact ==, but the unique index is on lower(email), so a
    different-cased duplicate slipped past the lookup and 500'd on INSERT. It must now
    match case-insensitively (reuse) and never 500."""
    pid = _product_id(client)

    def _post(email: str):
        return client.post(
            "/api/applicant/v1/applications",
            json={
                "patient_profile": {"legal_first_name": "Case", "email": email},
                "credit_product_id": pid,
                "requested_amount_cents": 3_000_000,
                "requested_amount_source": "patient",
                "contact_method": "email",
            },
        )

    assert _post("Mixed.Case@Example.com").status_code == 201
    assert _post("mixed.case@example.com").status_code == 201  # would have 500'd before


# --- dev seed-clinic duplicate email ---------------------------------------


def test_seed_clinic_duplicate_email_returns_409_not_500(client):
    """A caller-supplied email that already exists collided on User.email → 500.
    The duplicate must now be a clean 409."""
    body = {"email": "dup-clinic@example.com"}
    r1 = client.post("/api/clinic/v1/dev/seed-clinic", json=body)
    assert r1.status_code == 200, r1.text
    r2 = client.post("/api/clinic/v1/dev/seed-clinic", json=body)
    assert r2.status_code == 409, r2.text
