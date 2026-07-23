"""Dave's canonical Application Status Flow v1.00 — registry + transitions.

Source of truth: ``docs/dave_review_2026-07-21/PaySpyre - Application Status Flow
v1,00.pdf``. These tests pin the table itself (every status, its workplaces, its
actions, its API) so a future edit that silently drops one of Dave's actions
fails here rather than in a demo.

DB-free by construction: the registry is a code constant and the transitions
operate on a duck-typed application object.
"""
from types import SimpleNamespace

import pytest

from app.services import application_status as sm
from app.services.application_status import Action, CanonicalStatus, ExternalApi, Workplace
from app.services.flow_orchestrator import (
    CLOSED_STATUSES,
    InvalidStateTransition,
    VERIFICATION_GATE_STATUSES,
    mark_active,
    mark_agreement_signature,
    mark_agreement_signed,
    mark_application_verification_pending,
    mark_bank_verification_pending,
    mark_closed,
    mark_credit_report_pending,
    mark_offer_acceptance,
    mark_offer_accepted,
    mark_returned_for_reprocessing,
    mark_verification_gate,
)


def _app(status: str):
    return SimpleNamespace(status=status, status_updated_at=None)


# ---------------------------------------------------------------------------
# The registry IS Dave's table
# ---------------------------------------------------------------------------


class TestRegistryMatchesDavesTable:
    def test_linear_flow_order(self):
        """Pre-Origination → Origination → [3 gates] → Credit Underwriting →
        Offer Acceptance → Agreement Signature → Approved → Active."""
        by_order: dict[int, set[CanonicalStatus]] = {}
        for spec in sm.STATUS_REGISTRY.values():
            by_order.setdefault(spec.order, set()).add(spec.status)
        assert by_order[1] == {CanonicalStatus.PRE_ORIGINATION}
        assert by_order[2] == {CanonicalStatus.ORIGINATION}
        assert by_order[3] == set(sm.VERIFICATION_GATES)
        assert by_order[4] == {CanonicalStatus.CREDIT_UNDERWRITING}
        assert by_order[5] == {CanonicalStatus.OFFER_ACCEPTANCE}
        assert by_order[6] == {CanonicalStatus.AGREEMENT_SIGNATURE}
        assert by_order[7] == {CanonicalStatus.APPROVED}
        assert by_order[8] == {CanonicalStatus.ACTIVE}

    def test_the_three_gates_are_a_parallel_band(self):
        assert len(sm.VERIFICATION_GATES) == 3
        for gate in sm.VERIFICATION_GATES:
            spec = sm.STATUS_REGISTRY[gate]
            assert spec.parallel_group == "verification"
            assert spec.workplaces == (Workplace.UNDERWRITING,)
            # every gate offers Return for reprocessing + Cancel
            assert Action.RETURN_FOR_REPROCESSING in spec.actions
            assert Action.CANCEL in spec.actions

    def test_six_closed_states_hang_off_active(self):
        active = sm.STATUS_REGISTRY[CanonicalStatus.ACTIVE]
        assert set(active.closed_options) == {
            CanonicalStatus.REPAID, CanonicalStatus.RENEWED, CanonicalStatus.REFINANCED,
            CanonicalStatus.TRANSFERRED, CanonicalStatus.SETTLEMENT,
            CanonicalStatus.WRITTEN_OFF,
        }
        assert len(sm.CLOSED_STATUSES) == 6
        for closed in sm.CLOSED_STATUSES:
            assert sm.STATUS_REGISTRY[closed].is_terminal

    @pytest.mark.parametrize(
        "status,expected",
        [
            (CanonicalStatus.PRE_ORIGINATION,
             (Action.COMPLETE_APPLICATION, Action.CANCEL)),
            (CanonicalStatus.ORIGINATION,
             (Action.SUBMIT_FOR_CREDIT_UNDERWRITING, Action.CANCEL)),
            (CanonicalStatus.CREDIT_REPORT,
             (Action.REQUEST_CREDIT_REPORT_AUTHORIZATION,
              Action.RETURN_FOR_REPROCESSING, Action.CANCEL)),
            (CanonicalStatus.BANK_ACCOUNT_VERIFICATION,
             (Action.REQUEST_BANK_VERIFICATION,
              Action.RETURN_FOR_REPROCESSING, Action.CANCEL)),
            (CanonicalStatus.APPLICATION_VERIFICATION,
             (Action.REQUEST_ADDITIONAL_INFORMATION,
              Action.RETURN_FOR_REPROCESSING, Action.CANCEL)),
            (CanonicalStatus.CREDIT_UNDERWRITING,
             (Action.APPROVE, Action.REJECT, Action.CANCEL,
              Action.RETURN_FOR_REPROCESSING)),
            (CanonicalStatus.OFFER_ACCEPTANCE,
             (Action.CONTACT_APPLICANT, Action.REGISTER_OFFER_ACCEPTANCE,
              Action.CANCEL, Action.RETURN_FOR_REPROCESSING)),
            (CanonicalStatus.AGREEMENT_SIGNATURE,
             (Action.CONTACT_APPLICANT, Action.CANCEL)),
            (CanonicalStatus.APPROVED,
             (Action.ACTIVATE_LOAN, Action.REJECT, Action.CANCEL)),
            (CanonicalStatus.ACTIVE,
             (Action.MANUAL_PAYMENT, Action.CHARGE_PAYMENT,
              Action.RESTRUCTURE_HARDSHIP, Action.CHANGE_PAYMENT_SCHEDULE,
              Action.SET_CLOSED)),
        ],
    )
    def test_actions_verbatim_from_the_pdf(self, status, expected):
        assert sm.STATUS_REGISTRY[status].actions == expected

    @pytest.mark.parametrize(
        "status,workplaces",
        [
            (CanonicalStatus.PRE_ORIGINATION,
             (Workplace.APPLICATION_PAGE, Workplace.ORIGINATION)),
            (CanonicalStatus.ORIGINATION, (Workplace.ORIGINATION,)),
            (CanonicalStatus.CREDIT_UNDERWRITING, (Workplace.UNDERWRITING,)),
            (CanonicalStatus.APPROVED, (Workplace.SERVICING, Workplace.UNDERWRITING)),
            (CanonicalStatus.ACTIVE, (Workplace.SERVICING, Workplace.COLLECTIONS)),
        ],
    )
    def test_owning_workplaces(self, status, workplaces):
        assert sm.STATUS_REGISTRY[status].workplaces == workplaces

    def test_external_apis(self):
        """Equifax on Credit Report, Flinks on Bank Account Verification — and
        nowhere else."""
        with_api = {
            s: spec.apis for s, spec in sm.STATUS_REGISTRY.items() if spec.apis
        }
        assert with_api == {
            CanonicalStatus.CREDIT_REPORT: (ExternalApi.EQUIFAX_CANADA,),
            CanonicalStatus.BANK_ACCOUNT_VERIFICATION: (ExternalApi.FLINKS_CAPITAL,),
        }

    def test_every_status_has_preconditions_and_a_description(self):
        for spec in sm.STATUS_REGISTRY.values():
            assert spec.preconditions, spec.status
            assert spec.description, spec.status

    def test_registry_payload_is_json_serializable_and_ordered(self):
        import json

        payload = sm.registry_payload()
        json.dumps(payload)  # must not raise
        assert [p["order"] for p in payload] == sorted(p["order"] for p in payload)
        assert len(payload) == len(sm.STATUS_REGISTRY)


# ---------------------------------------------------------------------------
# Legacy → canonical mapping (nothing dropped)
# ---------------------------------------------------------------------------


class TestLegacyMapping:
    EXPECTED = {
        "started": CanonicalStatus.PRE_ORIGINATION,
        "origination": CanonicalStatus.ORIGINATION,
        "pre_qualified": CanonicalStatus.ORIGINATION,
        "verifying": CanonicalStatus.APPLICATION_VERIFICATION,
        "awaiting_hard_pull": CanonicalStatus.CREDIT_REPORT,
        "underwriting": CanonicalStatus.CREDIT_UNDERWRITING,
        "under_review": CanonicalStatus.CREDIT_UNDERWRITING,
        "approved": CanonicalStatus.APPROVED,
        "rejected": CanonicalStatus.REJECTED,
        "withdrawn": CanonicalStatus.CANCELLED,
        "expired": CanonicalStatus.EXPIRED,
    }

    @pytest.mark.parametrize("legacy,canonical", sorted(EXPECTED.items()))
    def test_every_legacy_value_maps(self, legacy, canonical):
        assert sm.canonical_for(legacy) is canonical

    def test_no_legacy_value_is_dropped(self):
        """Every value of the PG enum must resolve — a status with no canonical
        home would silently vanish from every queue."""
        from app.models.platform.credit_application import PlatformCreditApplication

        enum_values = set(PlatformCreditApplication.__table__.c.status.type.enums)
        unmapped = enum_values - set(sm.LEGACY_TO_CANONICAL)
        assert not unmapped, f"unmapped application statuses: {sorted(unmapped)}"

    def test_off_model_terminals_are_preserved_not_folded(self):
        """rejected / withdrawn(cancelled) / expired have no slot in Dave's
        forward flow and must NOT be collapsed into one another."""
        assert len({sm.canonical_for(s) for s in ("rejected", "withdrawn", "expired")}) == 3
        for canonical in sm.OFF_MODEL_TERMINALS:
            assert sm.STATUS_REGISTRY[canonical].is_terminal
            assert sm.STATUS_REGISTRY[canonical].note  # reasoning is documented

    def test_underwriting_and_under_review_share_a_canonical_status_but_not_a_value(self):
        assert sm.canonical_for("underwriting") is sm.canonical_for("under_review")
        assert sm.CANONICAL_TO_ENGINE[CanonicalStatus.CREDIT_UNDERWRITING] == "underwriting"

    def test_unknown_status_degrades_to_none_never_raises(self):
        assert sm.canonical_for("brand_new_enum_value") is None
        assert sm.spec_for("brand_new_enum_value") is None
        assert sm.actions_for("brand_new_enum_value") == ()
        assert sm.workplaces_for("brand_new_enum_value") == ()
        assert sm.is_terminal("brand_new_enum_value") is False

    def test_new_enum_values_present_on_the_model(self):
        from app.models.platform.credit_application import PlatformCreditApplication

        enum_values = set(PlatformCreditApplication.__table__.c.status.type.enums)
        assert {
            "credit_report", "bank_verification", "application_verification",
            "offer_acceptance", "agreement_signature", "active",
            "repaid", "renewed", "refinanced", "transferred", "settlement",
            "written_off",
        }.issubset(enum_values)
        # additive-only: every prior value is retained
        assert {
            "started", "origination", "verifying", "pre_qualified",
            "awaiting_hard_pull", "underwriting", "under_review", "approved",
            "rejected", "withdrawn", "expired",
        }.issubset(enum_values)


class TestActionLookup:
    def test_actions_for_resolves_through_legacy_values(self):
        assert Action.RETURN_FOR_REPROCESSING in sm.actions_for("under_review")
        assert sm.is_action_permitted("approved", Action.ACTIVATE_LOAN)
        assert not sm.is_action_permitted("approved", Action.SUBMIT_FOR_CREDIT_UNDERWRITING)

    def test_terminal_lookup(self):
        assert sm.is_terminal("rejected")
        assert sm.is_terminal("written_off")
        assert not sm.is_terminal("active")
        assert not sm.is_terminal("started")


# ---------------------------------------------------------------------------
# Orchestrator transitions
# ---------------------------------------------------------------------------


class TestVerificationGates:
    @pytest.mark.parametrize(
        "transition,expected",
        [
            (mark_credit_report_pending, "credit_report"),
            (mark_bank_verification_pending, "bank_verification"),
            (mark_application_verification_pending, "application_verification"),
        ],
    )
    def test_each_gate_sets_its_status(self, transition, expected):
        app = _app("origination")
        transition(app)
        assert app.status == expected
        assert app.status_updated_at is not None

    def test_gates_are_mutually_reachable(self):
        """The gates are parallel: a file may move between them freely."""
        app = _app("origination")
        for gate in VERIFICATION_GATE_STATUSES:
            mark_verification_gate(app, gate)
            assert app.status == gate

    def test_unknown_gate_rejected(self):
        app = _app("origination")
        with pytest.raises(InvalidStateTransition):
            mark_verification_gate(app, "not_a_gate")
        assert app.status == "origination"

    @pytest.mark.parametrize("terminal", ["approved", "rejected", "withdrawn", "expired",
                                          "active", "repaid", "written_off"])
    def test_gates_refuse_terminal_files(self, terminal):
        app = _app(terminal)
        with pytest.raises(InvalidStateTransition):
            mark_credit_report_pending(app)
        assert app.status == terminal


class TestOfferAndAgreement:
    def test_happy_path_to_approved(self):
        app = _app("under_review")
        mark_offer_acceptance(app)
        assert app.status == "offer_acceptance"
        mark_offer_accepted(app)          # incl. "manually register offer acceptance"
        assert app.status == "agreement_signature"
        mark_agreement_signed(app)
        assert app.status == "approved"

    def test_offer_acceptance_only_from_offer_acceptance(self):
        app = _app("under_review")
        with pytest.raises(InvalidStateTransition):
            mark_offer_accepted(app)
        assert app.status == "under_review"

    def test_agreement_signed_only_from_agreement_signature(self):
        app = _app("offer_acceptance")
        with pytest.raises(InvalidStateTransition):
            mark_agreement_signed(app)

    def test_mark_agreement_signature_refuses_terminal(self):
        app = _app("rejected")
        with pytest.raises(InvalidStateTransition):
            mark_agreement_signature(app)


class TestActivationAndClosure:
    def test_activate_from_approved(self):
        app = _app("approved")
        mark_active(app)
        assert app.status == "active"

    @pytest.mark.parametrize("source", ["started", "under_review", "agreement_signature",
                                        "active", "rejected"])
    def test_activate_only_from_approved(self, source):
        app = _app(source)
        with pytest.raises(InvalidStateTransition):
            mark_active(app)
        assert app.status == source

    @pytest.mark.parametrize("closed", CLOSED_STATUSES)
    def test_each_closed_state_reachable_from_active(self, closed):
        app = _app("active")
        mark_closed(app, closed)
        assert app.status == closed

    def test_closed_requires_active(self):
        app = _app("approved")
        with pytest.raises(InvalidStateTransition):
            mark_closed(app, "repaid")

    def test_unknown_closed_status_rejected(self):
        app = _app("active")
        with pytest.raises(InvalidStateTransition):
            mark_closed(app, "vanished")
        assert app.status == "active"

    def test_closed_states_match_the_registry(self):
        assert set(CLOSED_STATUSES) == {s.value for s in sm.CLOSED_STATUSES}


class TestReturnForReprocessing:
    @pytest.mark.parametrize(
        "source",
        ["credit_report", "bank_verification", "application_verification",
         "verifying", "awaiting_hard_pull", "underwriting", "under_review",
         "offer_acceptance"],
    )
    def test_returns_to_origination(self, source):
        app = _app(source)
        mark_returned_for_reprocessing(app)
        assert app.status == "origination"

    @pytest.mark.parametrize("source", ["started", "origination", "approved",
                                        "agreement_signature", "active", "rejected"])
    def test_refused_where_dave_does_not_offer_it(self, source):
        app = _app(source)
        with pytest.raises(InvalidStateTransition):
            mark_returned_for_reprocessing(app)
        assert app.status == source

    def test_every_status_offering_the_action_has_a_transition(self):
        """Registry and orchestrator must agree: if the UI may render
        "Return for reprocessing", the transition must accept that status."""
        from app.services import flow_orchestrator

        for engine_status, canonical in sm.LEGACY_TO_CANONICAL.items():
            offered = Action.RETURN_FOR_REPROCESSING in sm.STATUS_REGISTRY[canonical].actions
            accepted = engine_status in flow_orchestrator._RETURNABLE_STATUSES
            if offered and canonical is not CanonicalStatus.REJECTED:
                assert accepted, f"{engine_status} offers the action but has no transition"


class TestBackwardCompatibility:
    """The pre-existing transitions and guards are untouched."""

    def test_legacy_transitions_still_work(self):
        from app.services.flow_orchestrator import (
            mark_manual_review, mark_origination, mark_underwriting, mark_verification,
        )

        app = _app("started")
        mark_origination(app)
        assert app.status == "origination"
        mark_verification(app)
        assert app.status == "verifying"
        mark_underwriting(app)
        assert app.status == "underwriting"
        mark_manual_review(app)
        assert app.status == "under_review"

    def test_terminal_guard_still_covers_the_original_four(self):
        from app.services.flow_orchestrator import _TERMINAL_STATUSES

        assert {"approved", "rejected", "withdrawn", "expired"}.issubset(_TERMINAL_STATUSES)
        # plus the servicing terminals introduced with Dave's model
        assert "active" in _TERMINAL_STATUSES
        assert set(CLOSED_STATUSES).issubset(_TERMINAL_STATUSES)

    def test_clinic_status_map_covers_every_new_value(self):
        from app.api.clinic.v1.status_map import _CLINIC_STATUS, to_clinic_status
        from app.models.platform.credit_application import PlatformCreditApplication

        enum_values = set(PlatformCreditApplication.__table__.c.status.type.enums)
        assert enum_values.issubset(set(_CLINIC_STATUS))
        assert to_clinic_status("active") == "approved"
        assert to_clinic_status("credit_report") == "started"

    def test_borrower_banner_covers_every_new_value(self):
        from app.services.borrower_portal import _APPLICATION_BANNERS
        from app.models.platform.credit_application import PlatformCreditApplication

        enum_values = set(PlatformCreditApplication.__table__.c.status.type.enums)
        assert enum_values.issubset(set(_APPLICATION_BANNERS))
