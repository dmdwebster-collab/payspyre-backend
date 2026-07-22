"""Internal staff comments + the ``patient_id`` exposure — tests.

Two things are proved here:

1. **Fix 1** — ``patient_id`` is present on the admin application header, the
   admin application detail, and ``GET /admin/loans/{id}``, and the
   customer-profile list accepts ``?patient_id=`` and filters by it. Frontend
   surfaces were recovering this by scanning ``platform_events``; these are the
   contracts that replace that hack.
2. **Fix 2** — the Comments tab round-trips, is append-only (no PATCH/DELETE
   route exists), is audited to ``platform_events``, and is UNREACHABLE from
   the clinic (vendor) and borrower surfaces.

The vendor/borrower-invisibility assertions are route-table + import-graph
assertions rather than "call it and expect 403": a 403 test only proves the
guard on a route that EXISTS, whereas the product requirement is that no
vendor-reachable route to this data exists at all.

Run (this file only — the full suite hits a shared DB):
    python -m pytest tests/test_staff_comments.py -p no:warnings -q
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.api.clinic.v1.router import clinic_router
from app.api.v1.api import api_router
from app.core.auth import get_current_user
from app.db.base import get_db
from app.models.loan import Vendor
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.credit_product import PlatformCreditProduct
from app.models.platform.event import PlatformEvent
from app.models.platform.loan import PlatformLoan
from app.models.platform.patient import PlatformPatient
from app.models.user import User

_ADMIN = "/api/v1/admin"
_CLINIC = "/api/clinic/v1"


# --- seeding ---------------------------------------------------------------


def _product_id(db: Session):
    p = (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.code == "dental_full_arch_v1")
        .first()
    )
    assert p is not None, "seed product dental_full_arch_v1 missing"
    return p.id


def _mk_user(db: Session, first: str, last: str) -> User:
    user = User(
        email=f"{first.lower()}-{uuid.uuid4().hex[:8]}@example.com",
        first_name=first,
        last_name=last,
        is_active=True,
        is_verified=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _mk_vendor(db: Session) -> Vendor:
    vendor = Vendor(
        business_name="Kelowna Dental Centre",
        business_type="corporation",
        contact_name="Owner",
        email=f"clinic-{uuid.uuid4().hex[:8]}@example.com",
        phone="+15555550100",
        address_line1="1 Main St",
        city="Kelowna",
        province="BC",
        postal_code="V1Y0A1",
        status="active",
    )
    db.add(vendor)
    db.commit()
    db.refresh(vendor)
    return vendor


def _mk_application(db: Session, vendor_id) -> PlatformCreditApplication:
    patient = PlatformPatient(
        email=f"pt-{uuid.uuid4().hex[:8]}@example.com",
        legal_first_name="Jordan",
        legal_last_name="Lee",
    )
    db.add(patient)
    db.commit()
    db.refresh(patient)
    application = PlatformCreditApplication(
        patient_id=patient.id,
        credit_product_id=_product_id(db),
        credit_product_version=1,
        requested_amount_cents=1_800_000,
        requested_amount_source="clinic",
        status="under_review",
        vendor_id=vendor_id,
    )
    db.add(application)
    db.commit()
    db.refresh(application)
    return application


def _mk_loan(db: Session, application: PlatformCreditApplication) -> PlatformLoan:
    loan = PlatformLoan(
        application_id=application.id,
        patient_id=application.patient_id,
        principal_cents=application.requested_amount_cents,
        annual_rate_bps=1290,
        term_months=24,
        status="active",
        principal_balance_cents=application.requested_amount_cents,
    )
    db.add(loan)
    db.commit()
    db.refresh(loan)
    return loan


def _admin_principal(user: User):
    return SimpleNamespace(
        id=user.id,
        email=user.email,
        first_name=user.first_name,
        last_name=user.last_name,
        roles=[SimpleNamespace(role=SimpleNamespace(name="admin"))],
    )


@pytest.fixture
def app(db_session: Session):
    application = FastAPI()
    application.include_router(api_router, prefix="/api/v1")
    application.include_router(clinic_router, prefix="/api/clinic/v1")
    application.dependency_overrides[get_db] = lambda: db_session
    yield application
    application.dependency_overrides.clear()


@pytest.fixture(scope="module")
def served_routes() -> dict[str, set[str]]:
    """``{path: {http methods}}`` for everything the API actually SERVES.

    Read from the generated OpenAPI document rather than by walking
    ``router.routes``: FastAPI 0.139 made ``include_router`` lazy, so a parent
    router's ``.routes`` holds wrapper objects with no ``path`` and a naive
    walk finds NOTHING — passing locally on an older pin while proving nothing
    in CI (the identical trap documented at length in
    ``tests/test_vendor_visibility_fence.py``; this file hit it too, on the
    first CI run). The OpenAPI schema is generated from fully-materialized
    routes, so it reads the same under every FastAPI version, and
    ``test_the_comment_routes_are_discoverable_at_all`` fails loudly if it ever
    comes back empty.
    """
    served = FastAPI()
    served.include_router(api_router, prefix="/api/v1")
    served.include_router(clinic_router, prefix="/api/clinic/v1")
    return {path: set(ops) for path, ops in served.openapi()["paths"].items()}


@pytest.fixture
def admin_client(app, db_session: Session):
    user = _mk_user(db_session, "Dana", "Admin")
    app.dependency_overrides[get_current_user] = lambda: _admin_principal(user)
    return TestClient(app), user


# ===========================================================================
# Fix 1 — patient_id exposure
# ===========================================================================


class TestPatientIdExposure:
    def test_application_header_and_detail_carry_patient_id(
        self, admin_client, db_session
    ):
        client, _ = admin_client
        vendor = _mk_vendor(db_session)
        application = _mk_application(db_session, vendor.id)

        # admin_originations is mounted under /admin/applications.
        header = client.get(f"{_ADMIN}/applications/{application.id}/header")
        assert header.status_code == 200, header.text
        assert header.json()["patient_id"] == str(application.patient_id)

        detail = client.get(f"{_ADMIN}/applications/{application.id}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["patient_id"] == str(application.patient_id)

    def test_loan_detail_carries_patient_id(self, admin_client, db_session):
        client, _ = admin_client
        vendor = _mk_vendor(db_session)
        application = _mk_application(db_session, vendor.id)
        loan = _mk_loan(db_session, application)

        r = client.get(f"{_ADMIN}/loans/{loan.id}")
        assert r.status_code == 200, r.text
        assert r.json()["patient_id"] == str(application.patient_id)

    def test_customer_profile_list_filters_by_patient_id(
        self, admin_client, db_session
    ):
        client, _ = admin_client
        vendor = _mk_vendor(db_session)
        mine = _mk_application(db_session, vendor.id)
        other = _mk_application(db_session, vendor.id)
        for app_row in (mine, other):
            created = client.post(
                f"{_ADMIN}/customer-profiles",
                json={"patient_id": str(app_row.patient_id), "data": {}},
            )
            assert created.status_code in (200, 201), created.text

        r = client.get(
            f"{_ADMIN}/customer-profiles", params={"patient_id": str(mine.patient_id)}
        )
        assert r.status_code == 200, r.text
        payload = r.json()
        assert payload["total"] == 1
        assert [p["patient_id"] for p in payload["items"]] == [str(mine.patient_id)]

    def test_unknown_patient_id_filter_returns_empty_not_error(self, admin_client):
        client, _ = admin_client
        r = client.get(
            f"{_ADMIN}/customer-profiles", params={"patient_id": str(uuid.uuid4())}
        )
        assert r.status_code == 200, r.text
        assert r.json() == {"total": 0, "items": []}


# ===========================================================================
# Fix 2 — internal staff comments
# ===========================================================================


class TestStaffComments:
    def test_application_thread_round_trip_and_audit(self, admin_client, db_session):
        client, user = admin_client
        vendor = _mk_vendor(db_session)
        application = _mk_application(db_session, vendor.id)
        base = f"{_ADMIN}/applications/{application.id}/comments"

        assert client.get(base).json() == []

        r = client.post(base, json={"body": "Stips outstanding: proof of income."})
        assert r.status_code == 201, r.text
        posted = r.json()
        assert posted["subject"] == "application"
        assert posted["application_id"] == str(application.id)
        assert posted["loan_id"] is None
        assert posted["author_name"] == "Dana Admin"
        assert posted["author_user_id"] == str(user.id)
        assert posted["mine"] is True

        client.post(base, json={"body": "Received — clearing the stip."})
        thread = client.get(base).json()
        assert [c["body"] for c in thread] == [
            "Stips outstanding: proof of income.",
            "Received — clearing the stip.",
        ]

        events = (
            db_session.query(PlatformEvent)
            .filter(
                PlatformEvent.application_id == application.id,
                PlatformEvent.event_type == "staff_comment.posted",
            )
            # Explicit ordering: an unordered query returns rows in whatever
            # order the planner likes, which differs between a freshly seeded
            # local DB and CI's.
            .order_by(PlatformEvent.id.asc())
            .all()
        )
        assert len(events) == 2
        assert {e.patient_id for e in events} == {application.patient_id}
        assert [e.payload["subject"] for e in events] == ["application", "application"]
        assert events[0].payload["comment_id"] == posted["id"]
        assert events[0].payload["body_preview"].startswith("Stips outstanding")

    def test_loan_thread_is_separate_and_can_replay_the_application(
        self, admin_client, db_session
    ):
        client, _ = admin_client
        vendor = _mk_vendor(db_session)
        application = _mk_application(db_session, vendor.id)
        loan = _mk_loan(db_session, application)

        client.post(
            f"{_ADMIN}/applications/{application.id}/comments",
            json={"body": "Underwriting note"},
        )
        r = client.post(
            f"{_ADMIN}/loans/{loan.id}/comments", json={"body": "Servicing note"}
        )
        assert r.status_code == 201, r.text
        assert r.json()["subject"] == "loan"
        assert r.json()["loan_id"] == str(loan.id)

        # Default: loan tab shows loan comments only.
        loan_only = client.get(f"{_ADMIN}/loans/{loan.id}/comments").json()
        assert [c["body"] for c in loan_only] == ["Servicing note"]

        # Opt-in: replay the originating application's thread, merged in order.
        merged = client.get(
            f"{_ADMIN}/loans/{loan.id}/comments",
            params={"include_application": "true"},
        ).json()
        assert [c["body"] for c in merged] == ["Underwriting note", "Servicing note"]

        # The application tab is unaffected by the loan post.
        app_only = client.get(
            f"{_ADMIN}/applications/{application.id}/comments"
        ).json()
        assert [c["body"] for c in app_only] == ["Underwriting note"]

    def test_unknown_subject_404s_and_empty_body_422s(self, admin_client, db_session):
        client, _ = admin_client
        vendor = _mk_vendor(db_session)
        application = _mk_application(db_session, vendor.id)

        assert (
            client.get(f"{_ADMIN}/applications/{uuid.uuid4()}/comments").status_code
            == 404
        )
        assert client.get(f"{_ADMIN}/loans/{uuid.uuid4()}/comments").status_code == 404
        r = client.post(
            f"{_ADMIN}/applications/{application.id}/comments", json={"body": ""}
        )
        assert r.status_code == 422


class TestAppendOnly:
    """No route can mutate or remove a posted comment — the property that makes
    the tab usable as evidence. Asserted against the SERVED route table, so
    adding a PATCH/DELETE later fails here and forces the decision to be
    consciously re-made."""

    def test_no_mutating_routes_on_comment_paths(self, served_routes):
        offenders = {
            path: sorted(methods)
            for path, methods in served_routes.items()
            if path.endswith("/comments") and methods - {"get", "post", "head"}
        }
        assert offenders == {}, f"comments must be append-only, found: {offenders}"

    def test_model_has_no_edit_or_delete_columns(self):
        from app.models.platform.staff_comment import PlatformStaffComment

        cols = set(PlatformStaffComment.__table__.columns.keys())
        assert not cols & {"updated_at", "edited_at", "deleted_at", "redacted_at"}


class TestStaffOnlyFence:
    """Internal only: no vendor and no borrower surface can reach this data."""

    def test_the_comment_routes_are_discoverable_at_all(self, served_routes):
        """Guards the three tests below from passing VACUOUSLY on an empty
        route map — which is exactly how an earlier version of this fence
        silently proved nothing in CI."""
        comment_paths = {p for p in served_routes if p.endswith("/comments")}
        assert comment_paths == {
            f"{_ADMIN}/applications/{{application_id}}/comments",
            f"{_ADMIN}/loans/{{loan_id}}/comments",
        }, sorted(comment_paths)

    def test_comment_routes_are_mounted_under_admin_only(self, served_routes):
        stray = [p for p in served_routes if "comment" in p and not p.startswith(_ADMIN)]
        assert stray == [], stray

    def test_no_clinic_route_exposes_comments(self, served_routes):
        clinic = [
            p for p in served_routes if p.startswith(_CLINIC) and "comment" in p
        ]
        assert clinic == [], clinic

    def test_clinic_package_never_imports_the_comment_model(self):
        import importlib
        import pkgutil

        import app.api.clinic.v1 as clinic_pkg

        offenders = []
        for mod in pkgutil.walk_packages(
            clinic_pkg.__path__, prefix=clinic_pkg.__name__ + "."
        ):
            module = importlib.import_module(mod.name)
            for name, obj in vars(module).items():
                target = getattr(obj, "__module__", "") or getattr(obj, "__name__", "")
                if "staff_comment" in str(target):
                    offenders.append(f"{mod.name}.{name}")
        assert offenders == [], offenders

    def test_every_comment_handler_is_role_gated(self):
        import inspect

        from app.api.v1.endpoints import admin_staff_comments

        handlers = [
            admin_staff_comments.list_application_comments,
            admin_staff_comments.post_application_comment,
            admin_staff_comments.list_loan_comments,
            admin_staff_comments.post_loan_comment,
        ]
        for fn in handlers:
            guards = [
                p.default.dependency
                for p in inspect.signature(fn).parameters.values()
                if hasattr(p.default, "dependency")
            ]
            assert any(
                "require_roles" in getattr(g, "__qualname__", "") for g in guards
            ), f"{fn.__name__} is not gated by require_roles"


def test_migration_074_chain_pin():
    """074 chains onto 073, keeping the alembic history a single linear head.

    This migration was authored against ``072_settings_backend_gaps`` while the
    sibling branch ``feat/risk-score-model`` was still open. That branch merged
    first (PR #204), landing ``073_risk_score_model`` on main, so 074 was
    re-chained onto 073 — otherwise 072 would have had two children and
    ``alembic upgrade head`` would have failed on a forked chain.

    If the merge train re-chains this again, update BOTH the migration's
    ``down_revision`` and this pin together. The repo-wide fork detector lives
    in ``tests/test_risk_scoring.py::test_alembic_history_has_a_single_head``.
    """
    import importlib.util
    from pathlib import Path

    name = "074_staff_comments"
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.revision == "074_staff_comments"
    assert mod.down_revision == "073_risk_score_model"
