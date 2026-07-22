"""Dave's back-office origination gaps (2026-07-21 review, frontend PR #72).

Five gaps, five sections:

1. ``POST /admin/borrowers`` — nothing under ``/admin`` could mint a borrower, so
   back-office origination could only ever attach someone who had already come
   through the applicant journey. DB-backed (the service is the thing under test).
2. ``start_date`` / ``use_custom_first_due_date`` on the create-from-profile
   payload. Schema-level (DB-free) — the terms invariant is the contract.
3. ``submit_for_credit_underwriting`` / ``return_for_reprocessing`` — registry
   actions that had no endpoint. Includes the DECISION-PATH PROOF: the two new
   transitions cannot touch any status the decision engine owns.
4. The granular ``loan.write_off`` permission (maker-checker unchanged).
5. ``provider_name`` on the application header.
"""
from datetime import date, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.platform.event import PlatformEvent
from app.models.platform.patient import PlatformPatient
from app.services import application_status as sm
from app.services import customer_profile as profiles
from app.services import flow_orchestrator as flow
from app.services import staff_accounts


def _values(email="new.borrower@example.ca", phone="2505559999") -> dict:
    return {
        "personal": {
            "first_name": "Dana",
            "last_name": "Okonkwo",
            "date_of_birth": "1991-06-02",
            "citizenship": "canadian",
            "education": "college_university",
        },
        "contact": {"email": email, "main_phone": phone},
        "current_address": {
            "street_address": "88 Bernard Ave",
            "city": "Kelowna",
            "province": "BC",
            "postal_code": "V1Y6N2",
            "residential_status": "rent",
            "resided_since": (date.today() - timedelta(days=365 * 4)).isoformat(),
            "monthly_rent": "1600.00",
        },
        "primary_income": {
            "income_type": "employed_full_time",
            "net_monthly_income": "5200.00",
            "employer_name": "Okanagan Dental Group",
        },
    }


# ===========================================================================
# GAP 1 — admin borrower creation
# ===========================================================================


def test_create_borrower_mints_patient_and_profile(db_session):
    profile = profiles.create_borrower(
        db_session, values=_values(), actor="staff-7", source="staff"
    )

    patient = (
        db_session.query(PlatformPatient)
        .filter(PlatformPatient.id == profile.patient_id)
        .one()
    )
    # Identity is DERIVED from the registry payload — never supplied twice.
    assert patient.legal_first_name == "Dana"
    assert patient.legal_last_name == "Okonkwo"
    assert patient.dob == date(1991, 6, 2)
    assert patient.email == "new.borrower@example.ca"
    assert patient.phone_e164 == "2505559999"

    assert profile.version == 1
    assert profiles.get_profile_for_patient(db_session, patient.id) is not None


def test_create_borrower_audits_without_leaking_values(db_session):
    profile = profiles.create_borrower(db_session, values=_values(), actor="staff-7")
    event = (
        db_session.query(PlatformEvent)
        .filter(PlatformEvent.event_type == "borrower.created")
        .filter(PlatformEvent.patient_id == profile.patient_id)
        .one()
    )
    assert event.payload["origin"] == "back_office"
    assert event.actor == "staff-7"
    # Hard Rule #6 — keys only, never a field VALUE.
    assert "Okonkwo" not in str(event.payload)
    # The profile-creation audit row still fires: this reuses create_profile,
    # it is not a parallel path.
    assert (
        db_session.query(PlatformEvent)
        .filter(PlatformEvent.event_type == "customer_profile.created")
        .filter(PlatformEvent.patient_id == profile.patient_id)
        .count()
        == 1
    )


def test_sin_is_not_required(db_session):
    """Dave's legal mandate: SIN stays optional. A borrower without one is valid."""
    values = _values(email="nosin@example.ca", phone="2505550001")
    assert "identification" not in values
    profile = profiles.create_borrower(db_session, values=values, actor="staff-7")
    patient = (
        db_session.query(PlatformPatient)
        .filter(PlatformPatient.id == profile.patient_id)
        .one()
    )
    assert patient.sin_encrypted is None
    assert patient.sin_last3 is None


@pytest.mark.parametrize(
    "second_email,second_phone,matched_on",
    [
        ("dupe@example.ca", "2505550002", "email"),          # same email
        ("DUPE@EXAMPLE.CA", "2505550003", "email"),          # case-insensitive
        ("other@example.ca", "2505550002", "phone"),         # same phone
    ],
)
def test_duplicate_borrower_is_a_conflict_pointing_at_the_existing_profile(
    db_session, second_email, second_phone, matched_on
):
    first = profiles.create_borrower(
        db_session,
        values=_values(email="dupe@example.ca", phone="2505550002"),
        actor="staff-7",
    )
    before = db_session.query(PlatformPatient).count()

    with pytest.raises(profiles.DuplicateBorrowerError) as exc:
        profiles.create_borrower(
            db_session,
            values=_values(email=second_email, phone=second_phone),
            actor="staff-7",
        )

    assert exc.value.matched_on == matched_on
    assert exc.value.patient_id == first.patient_id
    assert exc.value.profile_id == first.id
    # Nothing was written on the conflict path — no silent duplicate borrower.
    assert db_session.query(PlatformPatient).count() == before


def test_invalid_payload_leaves_no_orphan_borrower(db_session):
    before = db_session.query(PlatformPatient).count()
    with pytest.raises(Exception):
        profiles.create_borrower(
            db_session,
            values={"personal": {"date_of_birth": "not-a-date"}},
            actor="staff-7",
        )
    db_session.rollback()
    assert db_session.query(PlatformPatient).count() == before


def test_find_duplicate_borrower_ignores_soft_deleted(db_session):
    profile = profiles.create_borrower(
        db_session,
        values=_values(email="gone@example.ca", phone="2505550004"),
        actor="staff-7",
    )
    patient = (
        db_session.query(PlatformPatient)
        .filter(PlatformPatient.id == profile.patient_id)
        .one()
    )
    from datetime import datetime, timezone

    patient.deleted_at = datetime.now(timezone.utc)
    db_session.commit()
    assert (
        profiles.find_duplicate_borrower(
            db_session, email="gone@example.ca", phone=None
        )
        is None
    )


# ===========================================================================
# GAP 2 — terms fields on the create-from-profile payload (DB-free)
# ===========================================================================


def _terms(**kw):
    from app.api.v1.endpoints.admin_customer_profiles import (
        ApplicationFromProfileRequest,
    )

    body = {
        "credit_product_id": uuid4(),
        "requested_amount_cents": 500_000,
    }
    body.update(kw)
    return ApplicationFromProfileRequest(**body)


class TestFinanceTermsPayload:
    def test_start_date_and_custom_first_due_date_are_accepted(self):
        body = _terms(
            start_date=date(2026, 8, 1),
            use_custom_first_due_date=True,
            first_due_date=date(2026, 9, 15),
        )
        assert body.start_date == date(2026, 8, 1)
        assert body.first_due_date == date(2026, 9, 15)

    def test_start_date_alone_is_fine(self):
        assert _terms(start_date=date(2026, 8, 1)).first_due_date is None

    def test_checkbox_without_a_date_is_rejected(self):
        with pytest.raises(Exception):
            _terms(start_date=date(2026, 8, 1), use_custom_first_due_date=True)

    def test_date_without_the_checkbox_is_rejected(self):
        with pytest.raises(Exception):
            _terms(start_date=date(2026, 8, 1), first_due_date=date(2026, 9, 1))

    @pytest.mark.parametrize("first_due", [date(2026, 8, 1), date(2026, 7, 1)])
    def test_first_due_must_follow_start_date(self, first_due):
        """Same invariant the main origination path enforces."""
        with pytest.raises(Exception):
            _terms(
                start_date=date(2026, 8, 1),
                use_custom_first_due_date=True,
                first_due_date=first_due,
            )


# ===========================================================================
# GAP 3 — the two registry actions, and the DECISION-PATH PROOF
# ===========================================================================


def _app(status: str):
    return SimpleNamespace(status=status, status_updated_at=None)


#: Statuses the decision engine owns or produces. A new origination transition
#: must never be able to move a file in one of these.
_DECISION_OWNED = (
    "approved", "declined", "withdrawn", "expired", "active", *flow.CLOSED_STATUSES,
)


def _module_post_paths(module) -> set[str]:
    """POST paths declared on an endpoint module's OWN router.

    DISCOVERY IS HERMETIC BY DESIGN — do NOT "simplify" this back to walking
    ``api_router.routes`` / ``app.routes`` / ``app.openapi()``. FastAPI 0.139
    made ``include_router`` LAZY: a PARENT router holds ``_IncludedRouter``
    wrappers that are never expanded into ``APIRoute`` objects (not by startup,
    not by entering the ``TestClient`` context, not by ``openapi()``), so the
    walk finds ZERO routes on CI while passing locally on an older pin. That is
    a silently blind test — twice before in this repo it vacuously "passed" a
    security assertion. See ``tests/test_clinic_authz.py::_clinic_routes`` and
    ``tests/test_vendor_visibility_fence.py`` for the same trap, documented.

    A module's own ``router`` is safe: the decorators attach real ``APIRoute``
    objects directly to it under every FastAPI version. The returned paths are
    RELATIVE to the module router — the mount prefix is asserted separately by
    ``test_routes_are_mounted_under_the_expected_prefixes``.
    """
    from fastapi.routing import APIRoute

    paths = {
        route.path
        for route in module.router.routes
        if isinstance(route, APIRoute) and "POST" in route.methods
    }
    assert paths, (
        f"Discovered ZERO POST routes on {module.__name__}.router — discovery is "
        "broken (blind test), not an empty module. See the docstring above."
    )
    return paths


class TestRegistryActionsHaveEndpoints:
    def test_both_actions_are_routed(self):
        from app.api.v1.endpoints import admin_originations

        paths = _module_post_paths(admin_originations)
        # Exact expected set, so an empty/partial map can never read as "fine".
        assert {
            "/{application_id}/submit-for-underwriting",
            "/{application_id}/return-for-reprocessing",
        } <= paths

    def test_borrower_creation_is_routed(self):
        from app.api.v1.endpoints import admin_customer_profiles

        assert "/borrowers" in _module_post_paths(admin_customer_profiles)

    def test_routes_are_mounted_under_the_expected_prefixes(self):
        """The mount prefix completes the public path the frontend wires against.

        Read STATICALLY from the api module's source: the mounted router object
        cannot be walked (lazy ``include_router``, see ``_module_post_paths``),
        but the prefix is a literal in the ``include_router`` call, and a moved
        mount is exactly the change that would silently break the frontend.
        """
        import inspect

        from app.api.v1 import api

        source = inspect.getsource(api)
        assert (
            'admin_originations.router, prefix="/admin/applications"' in source
        ), "admin_originations is no longer mounted at /admin/applications"
        assert (
            'admin_customer_profiles.router, prefix="/admin"' in source
        ), "admin_customer_profiles is no longer mounted at /admin"

    def test_every_registry_action_the_ui_renders_is_reachable(self):
        """The registry's whole point: a rendered action must be invocable.

        Pins the two this PR closes — if either is dropped from a status's action
        list, or a new status starts offering one, this fails loudly.
        """
        submit = {
            s
            for s, c in sm.LEGACY_TO_CANONICAL.items()
            if sm.Action.SUBMIT_FOR_CREDIT_UNDERWRITING
            in sm.STATUS_REGISTRY[c].actions
        }
        assert submit == {"origination", "pre_qualified"}
        returnable = {
            s
            for s, c in sm.LEGACY_TO_CANONICAL.items()
            if sm.Action.RETURN_FOR_REPROCESSING in sm.STATUS_REGISTRY[c].actions
        }
        # Everything the registry offers it on is accepted by the orchestrator,
        # except `declined` — deliberately routed to the decision-override
        # endpoint, which carries the reason-code and orphan-loan guards.
        assert returnable - {"declined"} <= set(flow._RETURNABLE_STATUSES)


class TestDecisionPathUnchanged:
    """PROOF: neither new transition can alter a decided or serviced file."""

    @pytest.mark.parametrize("status", _DECISION_OWNED)
    def test_submit_refuses_every_decision_owned_status(self, status):
        # 1. The registry never offers the action there, so the endpoint 409s
        #    before reaching the orchestrator.
        assert not sm.is_action_permitted(
            status, sm.Action.SUBMIT_FOR_CREDIT_UNDERWRITING
        )
        # 2. And the orchestrator refuses it anyway (defence in depth).
        app = _app(status)
        with pytest.raises(flow.InvalidStateTransition):
            flow.mark_verification(app)
        assert app.status == status

    @pytest.mark.parametrize("status", _DECISION_OWNED)
    def test_return_refuses_every_decision_owned_status(self, status):
        app = _app(status)
        with pytest.raises(flow.InvalidStateTransition):
            flow.mark_returned_for_reprocessing(app)
        assert app.status == status

    def test_declined_is_not_reopened_by_the_return_transition(self):
        """A credit decision is reversed only through the decision endpoint."""
        app = _app("declined")
        with pytest.raises(flow.InvalidStateTransition):
            flow.mark_returned_for_reprocessing(app)
        assert app.status == "declined"

    def test_submit_default_lands_on_the_pre_existing_band_status(self):
        """No gate named -> 'verifying', the value the automated journey already
        used. The default path therefore behaves exactly as before."""
        app = _app("origination")
        flow.mark_verification(app)
        assert app.status == "verifying"

    @pytest.mark.parametrize("gate", flow.VERIFICATION_GATE_STATUSES)
    def test_submit_may_name_one_of_daves_three_gates(self, gate):
        app = _app("origination")
        flow.mark_verification_gate(app, gate)
        assert app.status == gate

    def test_return_sends_the_file_back_to_origination(self):
        app = _app("under_review")
        flow.mark_returned_for_reprocessing(app)
        assert app.status == "origination"
        # …and Origination offers exactly the actions the bar should now render.
        assert sm.Action.SUBMIT_FOR_CREDIT_UNDERWRITING in sm.actions_for("origination")


def test_new_endpoints_do_not_write_status_directly():
    """The orchestrator owns every status write (spec §4.3).

    ``tests/test_application_status_writes.py`` enforces this repo-wide; this
    pins it for the module the new routes live in, so a future edit that reaches
    for ``app_row.status =`` fails in the file that introduced the temptation.
    """
    import inspect

    from app.api.v1.endpoints import admin_originations

    source = inspect.getsource(admin_originations)
    assert "app_row.status =" not in source
    assert "flow.mark_returned_for_reprocessing" in source
    assert "flow.mark_verification" in source


# ===========================================================================
# GAP 4 — granular loan.write_off permission
# ===========================================================================


class TestWriteOffPermission:
    def test_permission_is_grantable(self):
        assert "loan.write_off" in staff_accounts.WORKPLACE_PERMISSION_NAMES
        entry = next(
            p for p in staff_accounts.ALL_PERMISSIONS if p.name == "loan.write_off"
        )
        assert (entry.resource, entry.action) == ("loan", "write_off")
        assert len(entry.name) <= 100

    def test_daves_transcribed_grid_is_untouched(self):
        """The 19-box grid is a verbatim artefact; the granular tier is separate."""
        assert len(staff_accounts.WORKPLACE_PERMISSIONS) == 19
        assert "loan.write_off" not in {
            p.name for p in staff_accounts.WORKPLACE_PERMISSIONS
        }
        assert len(staff_accounts.ALL_PERMISSIONS) == 20

    def test_charge_off_route_is_permission_gated_not_role_gated(self):
        import inspect

        from app.api.v1.endpoints import admin_actions

        source = inspect.getsource(admin_actions.request_charge_off)
        assert 'require_permission_or_admin("loan", "write_off")' in source
        assert 'require_roles("admin")' not in source

    def test_maker_checker_second_approver_is_still_admin_only(self):
        """The permission decides who may INITIATE. The approver is the control."""
        import inspect

        from app.api.v1.endpoints import admin_actions

        for fn in (admin_actions.approve_action, admin_actions.reject_action):
            assert 'require_roles("admin")' in inspect.getsource(fn)

    def test_migration_seeds_the_permission_and_matches_the_service(self):
        import importlib.util
        from pathlib import Path

        path = (
            Path(__file__).resolve().parents[1]
            / "alembic"
            / "versions"
            / "075_write_off_permission.py"
        )
        spec = importlib.util.spec_from_file_location("m075", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.down_revision == "074_staff_comments"
        assert module._PERMISSION_NAME == "loan.write_off"
        entry = next(
            p for p in staff_accounts.GRANULAR_PERMISSIONS if p.name == "loan.write_off"
        )
        assert module._PERMISSION_RESOURCE == entry.resource
        assert module._PERMISSION_ACTION == entry.action


# ===========================================================================
# GAP 5 — provider_name on the header
# ===========================================================================


def test_header_returns_provider_name():
    from app.api.v1.endpoints.admin_originations import ApplicationHeader

    assert "provider_name" in ApplicationHeader.model_fields
    # Free text on the application; absent providers table means it may be null.
    assert ApplicationHeader.model_fields["provider_name"].default is None
