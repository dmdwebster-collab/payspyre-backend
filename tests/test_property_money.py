"""Property-based tests of the money core (Hypothesis).

Fintech practice (research-verified): assert INVARIANTS that must hold for ALL
inputs, and let Hypothesis generate thousands of cases — including the adversarial
edges a human wouldn't write. These guard the amortization engine, the marketplace
pricing engine, and the verification-depth ladder.
"""
from datetime import date

from hypothesis import given, settings
from hypothesis import strategies as st

from app.services.lead_metrics import derive_verification_depth, _DEPTH_RANK
from app.services.loan_servicing import generate_amortization_schedule
from app.services.marketplace.pricing import price_lead

_FIRST_DUE = date(2026, 7, 1)

# Realistic lending ranges + edges: $0.01 .. $1,000,000; 0% .. 50% APR; 1 .. 120 mo.
_principal = st.integers(min_value=1, max_value=100_000_000)
_rate_bps = st.integers(min_value=0, max_value=5000)
_term = st.integers(min_value=1, max_value=120)


# --- amortization schedule invariants --------------------------------------


@settings(max_examples=600)
@given(principal=_principal, rate_bps=_rate_bps, term=_term)
def test_amortization_invariants(principal, rate_bps, term):
    rows = generate_amortization_schedule(principal, rate_bps, term, _FIRST_DUE)

    # Structure
    assert len(rows) == term
    assert [r.installment_number for r in rows] == list(range(1, term + 1))

    # Money conservation — the books MUST tie out exactly (integer cents).
    assert sum(r.principal_cents for r in rows) == principal
    total_interest = sum(r.interest_cents for r in rows)
    assert sum(r.total_cents for r in rows) == principal + total_interest

    for r in rows:
        # Each installment's total is exactly its principal + interest.
        assert r.total_cents == r.principal_cents + r.interest_cents
        # No negative components — incl. the rounding-remainder final installment,
        # which must never over-amortize into a negative principal.
        assert r.principal_cents >= 0
        assert r.interest_cents >= 0


# --- marketplace pricing invariants ----------------------------------------

_LEAD_STATES = st.sampled_from(["unqualified", "pre_qualified", "pre_approved", "approved"])
_CATEGORIES = st.sampled_from(
    ["implants", "full_arch", "orthodontics", "general_dentistry", "unknown_cat", "😀"]
)
_URGENCY = st.sampled_from(["immediate", "this_week", "this_month", "flexible", "bogus"])
_DEPTH = st.sampled_from(
    ["none", "id_verified", "id_bank_verified", "id_bank_cb_verified", "weird"]
)


@settings(max_examples=400)
@given(
    lead_state=_LEAD_STATES,
    cats=st.lists(_CATEGORIES, max_size=6),
    urgency=_URGENCY,
    depth=_DEPTH,
)
def test_price_lead_invariants(lead_state, cats, urgency, depth):
    price = price_lead(
        lead_state=lead_state, treatment_categories=cats, urgency=urgency, verification_depth=depth
    )
    # Always a positive integer number of cents (a valid lead is never free/negative).
    assert isinstance(price, int)
    assert price > 0
    # Deterministic — same inputs, same price.
    again = price_lead(
        lead_state=lead_state, treatment_categories=cats, urgency=urgency, verification_depth=depth
    )
    assert price == again


@settings(max_examples=300)
@given(lead_state=_LEAD_STATES, cats=st.lists(_CATEGORIES, max_size=4), urgency=_URGENCY, depth=_DEPTH)
def test_price_lead_max_category_monotonic(lead_state, cats, urgency, depth):
    # full_arch is the highest category multiplier (2.5x). Adding it must NEVER
    # lower the price (max-category pricing is monotonic in the category set).
    base = price_lead(lead_state=lead_state, treatment_categories=cats, urgency=urgency, verification_depth=depth)
    with_full = price_lead(
        lead_state=lead_state, treatment_categories=cats + ["full_arch"], urgency=urgency, verification_depth=depth
    )
    assert with_full >= base


# --- verification-depth ladder monotonicity --------------------------------

_VERIFS = st.sampled_from(["kyc_id", "bank_link", "bureau_soft", "bureau_hard", "income_attestation"])


@settings(max_examples=300)
@given(base=st.sets(_VERIFS), extra=st.sets(_VERIFS))
def test_verification_depth_is_monotonic(base, extra):
    # Passing MORE verifications can only raise (never lower) the derived depth.
    superset = base | extra
    assert _DEPTH_RANK[derive_verification_depth(superset)] >= _DEPTH_RANK[derive_verification_depth(base)]
