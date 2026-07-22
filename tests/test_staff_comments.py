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
            .all()
        )
        assert len(events) == 2
        assert events[0].patient_id == application.patient_id
        assert events[0].payload["subject"] == "application"
        assert events[0].payload["comment_id"] == posted["id"]

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
    the tab usable as evidence. Asserted against the route table, so adding a
    PATCH/DELETE later fails here and forces the decision to be re-made."""

    def test_no_mutating_routes_on_comment_paths(self):
        offenders = [
            (path, sorted(methods))
            for path, methods in _all_routes(api_router)
            if path.endswith("/comments") and methods - {"GET", "POST", "HEAD"}
        ]
        assert offenders == [], f"comments must be append-only, found: {offenders}"

    def test_model_has_no_edit_or_delete_columns(self):
        from app.models.platform.staff_comment import PlatformStaffComment

        cols = set(PlatformStaffComment.__table__.columns.keys())
        assert not cols & {"updated_at", "edited_at", "deleted_at", "redacted_at"}


class TestStaffOnlyFence:
    """Internal only: no vendor and no borrower surface can reach this data."""

    def test_no_clinic_route_exposes_comments(self):
        paths = _all_paths(clinic_router)
        assert not [p for p in paths if "comment" in p], paths

    def test_no_borrower_portal_route_exposes_comments(self):
        from app.api.v1.api import api_router as admin_api

        borrower = [
            p
            for p in _all_paths(admin_api)
            if "comment" in p and not p.startswith("/admin")
        ]
        assert borrower == [], borrower

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

    def test_comment_routes_are_mounted_under_admin_only(self):
        comment_paths = [p for p in _all_paths(api_router) if p.endswith("/comments")]
        assert comment_paths, "expected the comment routes to be discoverable"
        assert all(p.startswith("/admin") for p in comment_paths), comment_paths


def _all_routes(router) -> list[tuple[str, set[str]]]:
    """Flatten a router into ``(path, methods)``, following lazily-included
    sub-routers.

    FastAPI 0.139 made ``include_router`` lazy, so a parent router's ``.routes``
    can hold wrapper objects with no ``path``; recurse through them rather than
    trusting a flat read (the same trap documented at length in
    ``tests/test_vendor_visibility_fence.py``).
    """
    out: list[tuple[str, set[str]]] = []
    for route in getattr(router, "routes", []):
        path = getattr(route, "path", None)
        if isinstance(path, str):
            out.append((path, set(getattr(route, "methods", set()) or set())))
        inner = getattr(route, "router", None)
        if inner is not None and inner is not router:
            prefix = getattr(route, "prefix", "") or ""
            out.extend((prefix + p, m) for p, m in _all_routes(inner))
    return out


def _all_paths(router) -> list[str]:
    return [p for p, _ in _all_routes(router)]
