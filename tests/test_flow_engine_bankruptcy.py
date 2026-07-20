"""WS-E bankruptcy discharge rule — pure unit tests (no DB, no network).

Matrix over :func:`app.services.flow_engine.resolve_bankruptcy_policy` plus
end-to-end ``run_flow`` integration with a stub bureau adapter, asserting:

* active/undischarged bankruptcy still hard-declines (the pre-existing
  protection is NEVER weakened — vendor override does not rescue it);
* discharged ≥ N years (config ``bureau.bankruptcy_discharge_min_years``,
  default 2) does not block;
* discharged < N years → DECLINED without a vendor override (current policy),
  MANUAL_REVIEW (never auto-approve, never ignore) with one.
"""
from __future__ import annotations

import copy
from datetime import date, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.adapters import (
    FlowAdapters,
    MockBankAdapter,
    MockVerificationAdapter,
    PatientProfile,
)
from app.services.adapters.base import BureauAdapter, BureauResult
from app.services.flow_engine import (
    DEFAULT_BANKRUPTCY_DISCHARGE_MIN_YEARS,
    REASON_ACTIVE_BANKRUPTCY,
    REASON_BANKRUPTCY_DISCHARGE_RECENT,
    resolve_bankruptcy_policy,
    run_flow,
)

AS_OF = date(2026, 7, 10)


def years_ago(n: float) -> date:
    return AS_OF - timedelta(days=int(n * 365.25))


# ---------------------------------------------------------------------------
# Pure matrix over resolve_bankruptcy_policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bankruptcy,discharged_at,min_years,vendor_override,expected",
    [
        # no bankruptcy → clear, override irrelevant
        (False, None, 2, False, "clear"),
        (False, None, 2, True, "clear"),
        # ACTIVE (no discharge date) → declined; vendor override does NOT rescue
        (True, None, 2, False, "declined"),
        (True, None, 2, True, "declined"),
        # discharged ≥ min_years → does not block
        (True, years_ago(3), 2, False, "clear"),
        (True, years_ago(2.01), 2, True, "clear"),
        (True, years_ago(10), 2, False, "clear"),
        # discharged < min_years → declined without override, manual_review with
        (True, years_ago(1), 2, False, "declined"),
        (True, years_ago(1), 2, True, "manual_review"),
        (True, years_ago(0.1), 2, True, "manual_review"),
        # config threading: a stricter min_years flips a 3-year discharge
        (True, years_ago(3), 5, False, "declined"),
        (True, years_ago(3), 5, True, "manual_review"),
        (True, years_ago(6), 5, False, "clear"),
        # future discharge date = not yet discharged → declined
        (True, AS_OF + timedelta(days=30), 2, True, "declined"),
    ],
)
def test_resolve_bankruptcy_policy_matrix(
    bankruptcy, discharged_at, min_years, vendor_override, expected
):
    got = resolve_bankruptcy_policy(
        bankruptcy=bankruptcy,
        discharged_at=discharged_at,
        min_years=min_years,
        vendor_override=vendor_override,
        as_of=AS_OF,
    )
    assert got == expected


def test_exact_boundary_discharged_exactly_min_years_is_clear():
    """Discharged exactly N calendar years ago (anniversary day) satisfies
    'minimum of N years' — the rule is calendar-exact, not days/365.25."""
    got = resolve_bankruptcy_policy(
        bankruptcy=True,
        discharged_at=AS_OF.replace(year=AS_OF.year - 2),  # 2024-07-10
        min_years=2,
        vendor_override=False,
        as_of=AS_OF,
    )
    assert got == "clear"
    # one day short of the anniversary → still gated
    got_short = resolve_bankruptcy_policy(
        bankruptcy=True,
        discharged_at=AS_OF.replace(year=AS_OF.year - 2) + timedelta(days=1),
        min_years=2,
        vendor_override=True,
        as_of=AS_OF,
    )
    assert got_short == "manual_review"


def test_default_min_years_is_two():
    # BUSINESS DEFAULT flagged for Dave — this pin makes any change deliberate.
    assert DEFAULT_BANKRUPTCY_DISCHARGE_MIN_YEARS == 2


# ---------------------------------------------------------------------------
# run_flow integration (stub adapters, engine stays pure)
# ---------------------------------------------------------------------------

MATRIX: dict = {
    "identity": {"required": True, "methods": ["id_doc_scan"], "min_confidence": 0.85},
    "income": {"required": True, "methods": ["bank_link"], "require_bank_link": True},
    "bureau": {"soft_pull_required": True, "hard_pull_required": False},
}


class StubBureau(BureauAdapter):
    def __init__(self, *, score=720, bankruptcy=False, discharged_at=None):
        self._score = score
        self._bankruptcy = bankruptcy
        self._discharged_at = discharged_at

    def _result(self, pull_type):
        return BureauResult(
            pull_type=pull_type,
            score=self._score,
            result="passed",
            bankruptcy=self._bankruptcy,
            bankruptcy_discharged_at=self._discharged_at,
        )

    async def soft_pull(self, patient):
        return self._result("soft")

    async def hard_pull(self, patient):
        return self._result("hard")


def make_inputs(matrix=None):
    application = SimpleNamespace(id=uuid4(), requested_amount_cents=1_000_000)
    product = SimpleNamespace(id=uuid4(), verification_matrix=copy.deepcopy(matrix or MATRIX))
    patient = PatientProfile(patient_id=uuid4(), province="BC", email="bk@example.com")
    return application, product, patient


def adapters_with(bureau: BureauAdapter) -> FlowAdapters:
    return FlowAdapters(
        verification=MockVerificationAdapter(forced_result="passed", forced_confidence=0.99),
        bureau=bureau,
        bank=MockBankAdapter(forced_result="passed"),
    )


async def test_active_bankruptcy_still_declines_even_with_vendor_override():
    """REGRESSION GUARD: the pre-existing active-bankruptcy hard decline is
    unchanged — vendor override does not weaken it."""
    application, product, patient = make_inputs()
    decision = await run_flow(
        application, product, patient,
        adapters_with(StubBureau(bankruptcy=True, discharged_at=None)),
        vendor_override=True,
    )
    assert decision.decision == "declined"
    assert REASON_ACTIVE_BANKRUPTCY in decision.decision_reasons


async def test_discharged_over_min_years_does_not_block():
    application, product, patient = make_inputs()
    decision = await run_flow(
        application, product, patient,
        adapters_with(StubBureau(bankruptcy=True, discharged_at=date.today() - timedelta(days=365 * 5))),
    )
    assert decision.decision == "approved"
    assert REASON_ACTIVE_BANKRUPTCY not in decision.decision_reasons
    assert REASON_BANKRUPTCY_DISCHARGE_RECENT not in decision.decision_reasons


async def test_recent_discharge_without_override_declines():
    application, product, patient = make_inputs()
    decision = await run_flow(
        application, product, patient,
        adapters_with(StubBureau(bankruptcy=True, discharged_at=date.today() - timedelta(days=200))),
    )
    assert decision.decision == "declined"
    assert REASON_BANKRUPTCY_DISCHARGE_RECENT in decision.decision_reasons


async def test_recent_discharge_with_vendor_override_routes_to_manual_review():
    """Dave's policy: hard-fail overlays still route to a human when a vendor
    may grant an override — never auto-approve, never ignore."""
    application, product, patient = make_inputs()
    decision = await run_flow(
        application, product, patient,
        adapters_with(StubBureau(bankruptcy=True, discharged_at=date.today() - timedelta(days=200))),
        vendor_override=True,
    )
    assert decision.decision == "manual_review"
    assert decision.next_state == "under_review"
    assert REASON_BANKRUPTCY_DISCHARGE_RECENT in decision.decision_reasons


async def test_min_years_config_is_threaded_from_product_matrix():
    """bureau.bankruptcy_discharge_min_years in the (snapshotted) product config
    drives the rule — config in, decision out."""
    matrix = copy.deepcopy(MATRIX)
    matrix["bureau"]["bankruptcy_discharge_min_years"] = 5
    application, product, patient = make_inputs(matrix)
    three_years = date.today() - timedelta(days=365 * 3 + 5)
    decision = await run_flow(
        application, product, patient,
        adapters_with(StubBureau(bankruptcy=True, discharged_at=three_years)),
    )
    assert decision.decision == "declined"  # 3y < 5y configured minimum

    decision_default = await run_flow(
        *make_inputs(),  # default config: 2y minimum
        adapters_with(StubBureau(bankruptcy=True, discharged_at=three_years)),
    )
    assert decision_default.decision == "approved"  # 3y ≥ 2y default


async def test_vendor_override_alone_never_flips_a_clean_decline():
    """The override flag ONLY affects the recent-discharge case — a below-floor
    score still declines with the flag set."""
    application, product, patient = make_inputs()
    decision = await run_flow(
        application, product, patient,
        adapters_with(StubBureau(score=520)),  # below the 600 floor, no bankruptcy
        vendor_override=True,
    )
    assert decision.decision == "declined"
