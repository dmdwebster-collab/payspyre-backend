"""Config-consumer tests — the anti-decorative-config contract.

Covers the two items from the 2026-07-21 remeasure audit:

  1. MONEY PATH — ``policy_config.allocation_priority.repayment`` is now walked
     by ``interest_engine.allocate_regular_payment``. The critical assertion in
     this file is that the DEFAULT is byte-identical to the historical
     hard-coded waterfall (``test_default_allocation_is_byte_identical_*``).
  2. The Flinks / Equifax behaviour blocks now reach their adapters, and every
     typed field is declared either CONSUMED or INFORMATIONAL.

DB-free by construction: pure functions, pydantic models and hand-built value
objects only. No Session, no fixtures, no network.
"""
from __future__ import annotations

import datetime as dt
import itertools

import pytest

from app.schemas.integration_config import (
    CONFIG_CONSUMERS,
    CONFIG_INFORMATIONAL,
    PROVIDER_CONFIG_SCHEMAS,
    EquifaxConfig,
    FlinksConfig,
    provider_config_field_metadata,
)
from app.schemas.product_policy_config import (
    ALLOCATION_CATEGORIES,
    DEFAULT_ACCRUAL_PRIORITY,
    DEFAULT_REPAYMENT_PRIORITY,
    ProductPolicyConfigError,
    POLICY_SECTION_STATUS,
    ProductPolicyConfig,
    allocation_priority_field_metadata,
    parse_product_policy_config,
    policy_config_field_metadata,
)
from app.services.interest_engine import (
    BalanceView,
    PaymentAllocation,
    allocate_payment,
    allocate_regular_payment,
)
from app.services import interest_engine

_AS_OF = dt.date(2026, 7, 22)


def _balances(**kw) -> BalanceView:
    base = dict(
        as_of=_AS_OF,
        outstanding_principal_cents=100_000,
        interest_due_cents=5_000,
        fees_due_cents=2_500,
        add_on_balance_cents=4_500,
    )
    base.update(kw)
    return BalanceView(**base)


# ---------------------------------------------------------------------------
# 1. MONEY PATH — default behaviour is unchanged
# ---------------------------------------------------------------------------


def _legacy_allocate_regular_payment(amount_cents: int, balances: BalanceView):
    """VERBATIM copy of the pre-change allocator (git 79f1bee).

    The oracle for "default behaviour must not change": every assertion below
    compares the new configurable allocator against this frozen implementation.
    """
    if amount_cents <= 0:
        raise ValueError("amount_cents must be positive")

    interest = min(amount_cents, balances.interest_due_cents)
    remaining = amount_cents - interest

    principal = min(remaining, balances.outstanding_principal_cents)
    remaining -= principal

    fees = min(remaining, balances.fees_due_cents)
    remaining -= fees

    return PaymentAllocation(
        interest_cents=interest,
        principal_cents=principal + remaining,
        fees_cents=fees,
        add_on_cents=0,
        overpayment_cents=remaining,
    )


# A grid that crosses every boundary of the waterfall: amounts below interest,
# straddling interest/principal, straddling principal/fees, exactly the total
# debt, and beyond it (overpayment).
_AMOUNTS = [1, 4_999, 5_000, 5_001, 6_000, 104_999, 105_000, 107_500, 107_501, 250_000]
_BALANCE_SHAPES = [
    {},
    {"interest_due_cents": 0},
    {"fees_due_cents": 0},
    {"outstanding_principal_cents": 0},
    {"interest_due_cents": 0, "fees_due_cents": 0, "outstanding_principal_cents": 0},
    {"add_on_balance_cents": 0},
    {"interest_due_cents": 1, "fees_due_cents": 1, "outstanding_principal_cents": 1},
]


@pytest.mark.parametrize("amount", _AMOUNTS)
@pytest.mark.parametrize("shape", _BALANCE_SHAPES)
def test_default_allocation_is_byte_identical_when_priority_is_none(amount, shape):
    """policy_config = NULL → the allocator behaves exactly as it did before."""
    b = _balances(**shape)
    assert allocate_regular_payment(amount, b) == _legacy_allocate_regular_payment(amount, b)


@pytest.mark.parametrize("amount", _AMOUNTS)
@pytest.mark.parametrize("shape", _BALANCE_SHAPES)
def test_default_allocation_is_byte_identical_with_the_default_order(amount, shape):
    """A product that SAVED the shipped default order allocates identically."""
    b = _balances(**shape)
    explicit = list(DEFAULT_REPAYMENT_PRIORITY)
    assert allocate_regular_payment(amount, b, explicit) == _legacy_allocate_regular_payment(
        amount, b
    )
    # …and through the dispatcher, which is what loan_servicing calls.
    assert allocate_payment("regular", amount, b, explicit) == _legacy_allocate_regular_payment(
        amount, b
    )
    assert allocate_payment("regular", amount, b, None) == _legacy_allocate_regular_payment(
        amount, b
    )


@pytest.mark.parametrize("amount", _AMOUNTS)
@pytest.mark.parametrize("shape", _BALANCE_SHAPES)
def test_the_shared_fee_swap_is_a_no_op(amount, shape):
    """origination ↔ administration is the one permitted reorder; because they
    share the engine's single fees_due bucket it must change nothing."""
    b = _balances(**shape)
    swapped = ["interest", "principal", "administration", "origination", "nsf"]
    assert allocate_regular_payment(amount, b, swapped) == _legacy_allocate_regular_payment(
        amount, b
    )


def test_every_configurable_order_allocates_identically_to_the_default():
    """The full set of orders the guards ACCEPT, exhaustively.

    This is the honest statement of what the repayment dialog can do today:
    under the retained guards the accepted set is the default order and its
    origination/administration swap, and both allocate identically. The list is
    now genuinely consumed (a hand-built non-default order below proves the
    engine reads it), but the *configurable* freedom is a no-op — which the UI
    metadata says out loud instead of implying otherwise.
    """
    accepted = []
    for perm in itertools.permutations(ALLOCATION_CATEGORIES):
        try:
            parse_product_policy_config({"allocation_priority": {"repayment": list(perm)}})
        except ProductPolicyConfigError:
            continue
        accepted.append(list(perm))

    assert sorted(accepted) == sorted(
        [
            ["interest", "principal", "origination", "administration", "nsf"],
            ["interest", "principal", "administration", "origination", "nsf"],
        ]
    )
    b = _balances()
    for order in accepted:
        assert allocate_regular_payment(6_000, b, order) == _legacy_allocate_regular_payment(
            6_000, b
        )


def test_the_engine_really_reads_the_order():
    """Guard against a silently-ignored argument: an order the guards would not
    accept still changes the allocation when handed straight to the engine."""
    b = _balances()
    principal_first = ["principal", "interest", "origination", "administration", "nsf"]
    alloc = allocate_regular_payment(6_000, b, principal_first)
    assert alloc.principal_cents == 6_000
    assert alloc.interest_cents == 0
    # …versus the default, which covers interest first.
    assert allocate_regular_payment(6_000, b).interest_cents == 5_000

    fees_before_principal = ["interest", "origination", "administration", "principal", "nsf"]
    alloc = allocate_regular_payment(6_000, b, fees_before_principal)
    assert alloc.interest_cents == 5_000
    assert alloc.fees_cents == 1_000
    assert alloc.principal_cents == 0


def test_a_non_permutation_priority_is_rejected_by_the_engine():
    b = _balances()
    with pytest.raises(ValueError, match="permutation"):
        allocate_regular_payment(1_000, b, ["interest", "principal"])
    with pytest.raises(ValueError, match="permutation"):
        allocate_regular_payment(1_000, b, ["interest", "interest", "principal",
                                            "origination", "administration"])


def test_engine_and_schema_vocabularies_are_in_sync():
    """interest_engine duplicates the category vocabulary to stay dependency-
    free; if the two ever drift the money would go somewhere the config
    doesn't describe."""
    assert interest_engine.ALLOCATION_CATEGORIES == ALLOCATION_CATEGORIES
    assert interest_engine.DEFAULT_REPAYMENT_PRIORITY == DEFAULT_REPAYMENT_PRIORITY


def test_non_regular_modes_ignore_the_priority():
    b = _balances()
    swapped = ["interest", "principal", "administration", "origination", "nsf"]
    assert allocate_payment("special", 1_000, b, swapped) == allocate_payment(
        "special", 1_000, b
    )
    assert allocate_payment("add_on", 1_000, b, swapped) == allocate_payment(
        "add_on", 1_000, b
    )
    assert allocate_payment("payoff", b.payoff_cents, b, swapped) == allocate_payment(
        "payoff", b.payoff_cents, b
    )


# ---------------------------------------------------------------------------
# loan_servicing resolution: loan → application → product → policy_config
# (stubbed session — no DB)
# ---------------------------------------------------------------------------


class _StubQuery:
    def __init__(self, row):
        self._row = row

    def filter(self, *a, **k):
        return self

    def first(self):
        return self._row


class _StubSession:
    """Returns a pre-canned row per queried model."""

    def __init__(self, rows: dict):
        self._rows = rows

    def query(self, model, *a, **k):
        return _StubQuery(self._rows.get(model))


class _Loan:
    id = "loan-1"

    def __init__(self, application_id):
        self.application_id = application_id


class _App:
    def __init__(self, credit_product_id):
        self.credit_product_id = credit_product_id


class _Product:
    def __init__(self, policy_config):
        self.id = "prod-1"
        self.policy_config = policy_config


def _session(policy_config, *, application_id="app-1", product_id="prod-1"):
    from app.models.platform.credit_application import PlatformCreditApplication
    from app.models.platform.credit_product import PlatformCreditProduct

    return _StubSession({
        PlatformCreditApplication: _App(product_id) if application_id else None,
        PlatformCreditProduct: _Product(policy_config) if product_id else None,
    })


def test_migrated_loan_without_an_application_uses_the_engine_default():
    from app.services.loan_servicing import resolve_repayment_priority

    assert resolve_repayment_priority(_session(None), _Loan(None)) is None


def test_null_policy_config_uses_the_engine_default():
    from app.services.loan_servicing import resolve_repayment_priority

    for raw in (None, {}):
        assert resolve_repayment_priority(_session(raw), _Loan("app-1")) is None


def test_stored_policy_config_reaches_the_allocator():
    from app.services.loan_servicing import resolve_repayment_priority

    raw = {
        "allocation_priority": {
            "repayment": ["interest", "principal", "administration", "origination", "nsf"]
        }
    }
    resolved = resolve_repayment_priority(_session(raw), _Loan("app-1"))
    assert resolved == ("interest", "principal", "administration", "origination", "nsf")


def test_a_corrupt_stored_policy_config_falls_back_instead_of_blocking_a_payment():
    """MONEY PATH fail-soft: a product row that no longer validates must never
    make a borrower's payment un-postable."""
    from app.services.loan_servicing import resolve_repayment_priority

    corrupt = {"allocation_priority": {"repayment": ["interest", "principal"]}}
    assert resolve_repayment_priority(_session(corrupt), _Loan("app-1")) is None


# ---------------------------------------------------------------------------
# Allocation-priority UI metadata (the "informational" declaration)
# ---------------------------------------------------------------------------


def test_allocation_priority_metadata_marks_accrual_read_only():
    meta = allocation_priority_field_metadata()
    assert meta["accrual"]["editable"] is False
    assert meta["accrual"]["informational"] is True
    assert meta["accrual"]["consumed_by"] is None
    assert meta["accrual"]["value"] == list(DEFAULT_ACCRUAL_PRIORITY)

    rep = meta["repayment"]
    assert rep["editable"] is True
    assert rep["informational"] is False
    assert rep["consumed_by"].endswith("allocate_regular_payment")
    # The only reorder the guards accept must be the one the UI advertises.
    assert rep["swappable_groups"] == [["administration", "origination"]]
    assert set(rep["pinned"]) == {"interest", "principal", "nsf"}


def test_every_policy_section_declares_a_status():
    """Audit guard: a new top-level policy_config section must declare whether
    anything consumes it, so the 'editable, persisted, ignored' state can never
    be introduced silently again."""
    declared = set(POLICY_SECTION_STATUS)
    actual = set(ProductPolicyConfig.model_fields) - {"schema_version"}
    assert actual - declared == set(), f"undeclared policy sections: {actual - declared}"
    assert declared - actual == set(), f"stale policy-section declarations: {declared - actual}"
    for name, entry in POLICY_SECTION_STATUS.items():
        assert entry["status"] in ("consumed", "informational", "pending_consumer"), name
        assert entry["note"]


def test_policy_config_field_metadata_shape():
    meta = policy_config_field_metadata()
    assert meta["sections"]["allocation_priority"]["status"] == "consumed"
    assert meta["sections"]["payoff"]["status"] == "consumed"
    assert meta["allocation_priority_fields"]["accrual"]["editable"] is False


# ---------------------------------------------------------------------------
# 2. Integration behaviour knobs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", sorted(PROVIDER_CONFIG_SCHEMAS))
def test_every_typed_field_is_declared(provider):
    """THE RULE: editable-and-consumed, or declared informational. Never a
    third state. A new field added to FlinksConfig/EquifaxConfig without an
    entry in CONFIG_CONSUMERS or CONFIG_INFORMATIONAL fails here."""
    model = PROVIDER_CONFIG_SCHEMAS[provider]
    consumers = CONFIG_CONSUMERS.get(provider, {})
    informational = CONFIG_INFORMATIONAL.get(provider, {})
    undeclared = [
        f for f in model.model_fields if f not in consumers and f not in informational
    ]
    assert undeclared == [], (
        f"{provider} config fields with neither a consumer nor an informational "
        f"declaration: {undeclared}"
    )
    # A field must not claim both.
    assert not (set(consumers) & set(informational))


@pytest.mark.parametrize("provider", sorted(PROVIDER_CONFIG_SCHEMAS))
def test_field_metadata_covers_every_field(provider):
    meta = provider_config_field_metadata(provider)
    assert set(meta) == set(PROVIDER_CONFIG_SCHEMAS[provider].model_fields)
    for name, entry in meta.items():
        if entry["informational"]:
            assert entry["consumed_by"] is None
            assert entry["reason"]
        else:
            assert entry["consumed_by"], f"{provider}.{name} declared consumed but unnamed"


def test_unknown_provider_has_no_field_metadata():
    assert provider_config_field_metadata("sendgrid") == {}


def test_transaction_depth_days_drives_the_income_window():
    """FlinksConfig.transaction_depth_days is the income-detection lookback."""
    from app.services.bank.transaction_analysis import INCOME_LOOKBACK_DAYS, analyze_accounts

    today = dt.date(2026, 7, 22)

    def _deposit(days_ago: int, cents: int) -> dict:
        d = today - dt.timedelta(days=days_ago)
        return {"Description": "PAYROLL DEPOSIT", "Credit": cents / 100,
                "Date": d.isoformat(), "Balance": 1000.0}

    # A bi-weekly payroll stream that sits ENTIRELY outside a 30-day window but
    # inside the 90-day default.
    accounts = [{
        "Balance": {"Current": 1000.0},
        "Transactions": [_deposit(d, 200_000) for d in (40, 54, 68, 82)],
    }]

    default_window = analyze_accounts(accounts, today=today)
    narrow_window = analyze_accounts(accounts, today=today, lookback_days=30)

    assert default_window["monthly_income_cents"] > 0
    assert narrow_window["monthly_income_cents"] == 0
    # None == the hard-coded default: unchanged behaviour.
    assert analyze_accounts(accounts, today=today, lookback_days=None) == default_window
    assert analyze_accounts(
        accounts, today=today, lookback_days=INCOME_LOOKBACK_DAYS
    ) == default_window


def test_equifax_environment_selects_the_base_url():
    from app.services.credit_bureau import EquifaxClient

    # Default (no config) keeps the historical production origin — unchanged.
    assert EquifaxClient(api_key="k").base_url == "https://api.equifax.ca/v1"
    assert EquifaxClient(api_key="k", environment="test").base_url.startswith(
        "https://api.sandbox.equifax.ca"
    )
    assert (
        EquifaxClient(api_key="k", environment="production").base_url
        == "https://api.equifax.ca/v1"
    )


def test_equifax_subscriber_identifiers_reach_the_request_payload():
    from app.services.credit_bureau import EquifaxClient

    bare = EquifaxClient(api_key="k")
    assert bare._subscriber_block() == {}  # unconfigured → payload unchanged

    configured = EquifaxClient(api_key="k", member_number="999", customer_code="ABC")
    assert configured._subscriber_block() == {
        "subscriber": {"member_number": "999", "customer_code": "ABC"}
    }


def test_equifax_settings_row_flows_into_the_client():
    """_client_for consumes the SAME settings row the api_key comes from."""
    from app.services.verifications.bureau_adapter import _client_for

    class _Row:
        config = {
            "environment": "test",
            "member_number": "M1",
            "customer_code": "C1",
            "log_request": True,
            "log_response": True,
        }

    client = _client_for("equifax", "k", _Row())
    assert client.environment == "test"
    assert client.member_number == "M1"
    assert client.customer_code == "C1"
    assert client.log_request is True and client.log_response is True

    # No row → previous behaviour exactly.
    legacy = _client_for("equifax", "k", None)
    assert legacy.base_url == "https://api.equifax.ca/v1"
    assert legacy.member_number is None
    assert legacy.log_request is False


def test_bank_verification_policy_shape_matches_the_flinks_defaults():
    from app.services import integration_behaviour

    policy = integration_behaviour.bank_verification_policy(None)
    defaults = FlinksConfig()
    assert policy == {
        "when_to_verify": defaults.when_to_verify.value,
        "allow_customer_skip": defaults.allow_customer_skip,
        "verification_expiry_days": defaults.verification_expiry_days,
        "verification_reminder_days": defaults.verification_reminder_days,
    }


def test_behaviour_resolver_falls_back_to_defaults_without_a_session():
    from app.services import integration_behaviour

    assert integration_behaviour.flinks_config(None) == FlinksConfig()
    assert integration_behaviour.equifax_config(None) == EquifaxConfig()
    # No row → test_mode must NOT force the simulator (that would be a
    # behaviour change for a tenant that never opened the Integrations page).
    assert integration_behaviour.flinks_forces_simulator(None) is False
