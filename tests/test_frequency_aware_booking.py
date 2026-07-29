"""FREQUENCY-AWARE LOAN BOOKING — the money-path gap this suite closes.

Before migration 082 the platform let a user originate a Weekly / Bi-Weekly /
Semi-Monthly loan, showed them a correct frequency-aware preliminary schedule at
``POST /admin/origination/quote``, printed that frequency on the loan agreement
they signed — and then BOOKED A MONTHLY LOAN, because
``loan_servicing.generate_amortization_schedule`` stepped in months only and
``platform_loans`` had nowhere to record a cadence. The booking chokepoint
emitted a ``booking_frequency_downgraded_to_monthly`` warning on every such deal;
that warning is gone because the downgrade is.

The proof obligations, in order of what they protect:

  1. THE CEO'S OWN VALIDATED EXAMPLE BOOKS. His servicing workbook
     (docs/dave_review_2026-07-21/AMOUNT_TO_MOVE_MODEL.md, oracled in
     ``tests/test_servicing_status.py``) is $10,000 / 48 months / 12.99% /
     $123.45 installment, **bi-weekly**, first due 2026-07-31. The exact
     scenario he checked our math against could not previously be booked at all.
  2. HIS WORKBOOK SERVICING FIGURES STILL HOLD against the schedule a REAL
     booking produces — Account Due As Of 2026-12-18, Amount to Move $123.45,
     DPD 0 at as-of 2026-10-09 — not just against a synthetic date list.
  3. QUOTE == BOOKED, ROW FOR ROW, at every frequency. The preliminary schedule
     the borrower is shown IS the schedule they get. That equality is the point.
  4. MONTHLY IS BIT-FOR-BIT UNCHANGED. Frequency defaults to monthly, so every
     pre-existing caller and every booked monthly loan is untouched.
  5. APR MOVES WITH FREQUENCY when a per-payment fee exists — a $1/payment admin
     fee is charged 104 times bi-weekly and 48 times monthly, and the disclosed
     Cost of Borrowing APR must reflect that.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.credit_product import PlatformCreditProduct
from app.models.platform.patient import PlatformPatient
from app.schemas.pricing_config import PaymentFrequency, payments_in_term
from app.services import loan_quote, origination_constraints as oc
from app.services import servicing_status as ss
from app.services.loan_servicing import (
    create_loan_from_application,
    generate_amortization_schedule,
    resolve_frequency,
)

ALL_FREQUENCIES = ["monthly", "semi_monthly", "bi_weekly", "weekly"]


# ===========================================================================
# 1. The CEO's bi-weekly example — the scenario that could not be booked
# ===========================================================================

# His inputs, verbatim from AMOUNT_TO_MOVE_MODEL.md / test_servicing_status.py.
CEO_PRINCIPAL_CENTS = 1_000_000      # $10,000.00
CEO_TERM_MONTHS = 48
CEO_RATE_BPS = 1299                  # 12.99%/yr
CEO_FIRST_DUE = date(2026, 7, 31)
CEO_INSTALLMENT_CENTS = 12_345       # $123.45 — his workbook's stated instalment
CEO_MOVE_BPS = 5_000                 # move_pct 0.50
CEO_AS_OF = date(2026, 10, 9)

# His ledger cash events (date, cash_cents, is_deferment).
CEO_LEDGER = [
    (date(2026, 7, 31), 6_000, False),
    (date(2026, 8, 14), 18_690, False),
    (date(2026, 8, 25), 0, True),        # deferment: +1 instalment of virtual credit
    (date(2026, 8, 28), 12_345, False),
    (date(2026, 9, 11), 12_345, False),
    (date(2026, 9, 25), 50_000, False),
    (date(2026, 10, 9), 5_553, False),
]


def _ceo_schedule():
    return generate_amortization_schedule(
        CEO_PRINCIPAL_CENTS,
        CEO_RATE_BPS,
        CEO_TERM_MONTHS,
        CEO_FIRST_DUE,
        frequency="bi_weekly",
    )


def test_ceo_bi_weekly_example_produces_a_bi_weekly_schedule():
    """104 instalments, 14 days apart — not 48 monthly ones.

    48 months bi-weekly is ``round(48 * 26 / 12) == 104`` payments (weeks do not
    divide months evenly, so the year fraction is the only honest conversion),
    and every gap is exactly 14 days.
    """
    rows = _ceo_schedule()

    assert len(rows) == 104
    assert payments_in_term(CEO_TERM_MONTHS, "bi_weekly") == 104
    assert rows[0].due_date == CEO_FIRST_DUE
    gaps = {
        (rows[i + 1].due_date - rows[i].due_date).days for i in range(len(rows) - 1)
    }
    assert gaps == {14}, "every bi-weekly step is exactly 14 days"
    assert rows[-1].due_date == date(2030, 7, 12)


def test_ceo_example_ties_out_to_the_cent():
    """The rounding / final-payment convention, at a non-monthly frequency.

    The regular instalment is the annuity payment rounded to whole cents, so
    drift accumulates over 104 rows; the FINAL instalment absorbs ALL of it, so
    principal sums EXACTLY and no fractional cent escapes.
    """
    rows = _ceo_schedule()

    assert sum(r.principal_cents for r in rows) == CEO_PRINCIPAL_CENTS
    assert sum(r.total_cents for r in rows) == CEO_PRINCIPAL_CENTS + sum(
        r.interest_cents for r in rows
    )
    # Exactly one row differs from the regular instalment: the last.
    regular = rows[0].total_cents
    assert [r.total_cents for r in rows[:-1]] == [regular] * 103
    assert rows[-1].total_cents != regular, "the final row carries the remainder"
    assert rows[-1].principal_cents > 0


def test_ceo_workbook_servicing_figures_hold_against_the_BOOKED_schedule():
    """His Account-Due-As-Of / Amount-to-Move oracle, driven off a REAL schedule.

    ``tests/test_servicing_status.py`` proves the model against a hand-written
    list of bi-weekly dates. This proves the SAME figures against the due dates
    the booking engine actually emits — which is the thing that was impossible
    before, because booking emitted 48 monthly dates for this deal.

    His stated instalment ($123.45) is the input here, as it is in his workbook.
    See ``test_the_platform_rate_convention_differs_from_the_workbook_instalment``
    for the (separate, documented) reason the engine derives $123.52 from the
    12.99% rate instead.
    """
    schedule_due_dates = [r.due_date for r in _ceo_schedule()]

    paid_virtual = 0
    final_moved = 0
    final_aoda = CEO_FIRST_DUE
    for _eff, cash, is_deferment in CEO_LEDGER:
        paid_virtual += cash + (CEO_INSTALLMENT_CENTS if is_deferment else 0)
        final_moved = ss.installments_moved(
            paid_virtual, CEO_INSTALLMENT_CENTS, CEO_MOVE_BPS
        )
        final_aoda = ss.advance_due_date(
            CEO_FIRST_DUE, final_moved, "bi_weekly", schedule_due_dates
        )

    assert paid_virtual == 117_278, "paid to date virtual"
    assert final_aoda == date(2026, 12, 18), "Account Due As Of"
    assert (
        ss.amount_to_move_cents(
            final_moved, CEO_INSTALLMENT_CENTS, CEO_MOVE_BPS, paid_virtual
        )
        == 12_345
    ), "Amount to Move ($123.45)"
    assert ss.days_past_due(CEO_AS_OF, final_aoda, 924_252) == 0, "DPD"
    assert (
        ss.next_scheduled_payment_date(CEO_AS_OF, schedule_due_dates) == date(2026, 10, 23)
    ), "next scheduled payment"


def test_the_platform_rate_convention_differs_from_the_workbook_instalment():
    """DOCUMENTED DIVERGENCE, pinned so it cannot drift silently.

    The engine derives $123.52 for his terms; his workbook says $123.45. The
    7-cent gap is a PERIODIC-RATE convention difference, not a frequency bug:

      * PaySpyre (here, and in ``/quote``, and in the APR math) uses
        ``annual_rate / payments_per_year`` = 12.99% / 26.
      * His sheet's number corresponds to ``annual_rate * 14 / 365`` (an
        actual/365 bi-weekly rate), which yields $123.4422.

    Changing the platform convention would move the instalment, the disclosed
    APR and the quote for EVERY loan at EVERY frequency, so it is a business
    decision for the CEO, not a side effect of this workstream. It is flagged in
    the PR body.

    It matters more than 7 cents looks: his worked example sits 0.5 cents above
    the threshold that advances Account-Due-As-Of a tenth installment, so the
    derived instalment moves his AODA one period earlier (2026-12-04). That knife
    edge is exactly why the divergence is pinned here rather than left implicit.
    """
    rows = _ceo_schedule()
    assert rows[0].total_cents == 12_352, "engine instalment at annual/26"

    # The knife edge, made explicit: the threshold that advances the account to
    # its 10th installment is 10*123.45 - 0.5*123.45 = $1,172.775, and his
    # paid-to-date-virtual is $1,172.78 — half a cent clear.
    assert ss.installments_moved(117_278, CEO_INSTALLMENT_CENTS, CEO_MOVE_BPS) == 10
    assert ss.installments_moved(117_277, CEO_INSTALLMENT_CENTS, CEO_MOVE_BPS) == 9

    derived = rows[0].total_cents
    paid_virtual_derived = sum(c for _d, c, _f in CEO_LEDGER) + derived
    moved_derived = ss.installments_moved(paid_virtual_derived, derived, CEO_MOVE_BPS)
    assert moved_derived == 9, "the derived instalment moves the account 9, not 10"


# ===========================================================================
# 2. Booking end-to-end: the frequency is read, used and PERSISTED
# ===========================================================================


def _product_id(db: Session):
    p = (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.code == "dental_full_arch_v1")
        .first()
    )
    assert p is not None
    return p.id


def _bookable_app(db: Session, *, decision=None, preferred=None, amount=1_000_000):
    patient = PlatformPatient(
        email=f"freq-{uuid.uuid4().hex[:8]}@example.com",
        legal_first_name="Robin",
        legal_last_name="Sandoval",
    )
    db.add(patient)
    db.commit()
    db.refresh(patient)
    application = PlatformCreditApplication(
        patient_id=patient.id,
        credit_product_id=_product_id(db),
        credit_product_version=1,
        requested_amount_cents=amount,
        requested_amount_source="clinic",
        status="approved",
        decision=decision,
        preferred_payment_frequency=preferred,
    )
    db.add(application)
    db.commit()
    db.refresh(application)
    return application


def test_the_ceo_example_BOOKS_bi_weekly_end_to_end(db_session: Session):
    """The headline: his deal is now bookable, and books as a bi-weekly loan."""
    application = _bookable_app(
        db_session,
        decision={
            "apr_bps": CEO_RATE_BPS,
            "term_months": CEO_TERM_MONTHS,
            "amount_cents": CEO_PRINCIPAL_CENTS,
            "payment_frequency": "bi_weekly",
        },
    )
    loan = create_loan_from_application(
        db_session, application, first_due_date=CEO_FIRST_DUE
    )

    assert loan.payment_frequency == "bi_weekly", "the cadence is PERSISTED"
    assert loan.term_months == 48, "term stays expressed in MONTHS (the contract unit)"

    schedule = sorted(loan.schedule, key=lambda s: s.installment_number)
    assert len(schedule) == 104
    assert schedule[0].due_date == CEO_FIRST_DUE
    assert {
        (schedule[i + 1].due_date - schedule[i].due_date).days
        for i in range(len(schedule) - 1)
    } == {14}
    assert sum(s.principal_cents for s in schedule) == CEO_PRINCIPAL_CENTS
    # The booked rows ARE the pure engine's rows.
    assert [(s.principal_cents, s.interest_cents, s.total_cents) for s in schedule] == [
        (r.principal_cents, r.interest_cents, r.total_cents) for r in _ceo_schedule()
    ]


def test_servicing_reads_the_BOOKED_loans_stored_frequency(db_session: Session):
    """``build_servicing_status`` services the booked loan on its real cadence."""
    application = _bookable_app(
        db_session,
        decision={
            "apr_bps": CEO_RATE_BPS,
            "term_months": CEO_TERM_MONTHS,
            "amount_cents": CEO_PRINCIPAL_CENTS,
            "payment_frequency": "bi_weekly",
        },
    )
    loan = create_loan_from_application(
        db_session, application, first_due_date=CEO_FIRST_DUE
    )

    status = ss.build_servicing_status(db_session, loan, CEO_AS_OF)
    assert status is not None
    # Nothing paid yet -> the account has earned through nothing, so Account Due
    # As Of sits on the first bi-weekly due date and DPD is measured off it.
    assert status.account_due_as_of == CEO_FIRST_DUE
    assert status.days_past_due == (CEO_AS_OF - CEO_FIRST_DUE).days
    # The next scheduled payment is a BI-WEEKLY step, not a monthly one.
    assert status.next_scheduled_payment_date == date(2026, 10, 23)


@pytest.mark.parametrize("frequency", ALL_FREQUENCIES)
def test_booking_persists_every_frequency_from_the_application(
    db_session: Session, frequency
):
    """The application's own ``preferred_payment_frequency`` reaches the loan
    even when no offer was issued (the direct approve-time booking path)."""
    application = _bookable_app(
        db_session,
        decision={"apr_bps": 1299, "term_months": 24},
        preferred=frequency,
    )
    loan = create_loan_from_application(db_session, application)

    assert loan.payment_frequency == frequency
    assert len(loan.schedule) == payments_in_term(24, frequency)


def test_the_accepted_offers_frequency_WINS_over_the_application(db_session: Session):
    """Precedence: the accepted deal beats the original origination preference.

    The decision record is how the accepted offer's amount/rate/term already
    reach booking; the frequency rides the same channel, so a file whose offer
    was written bi-weekly books bi-weekly even if intake said monthly.
    """
    application = _bookable_app(
        db_session,
        decision={
            "apr_bps": 1299,
            "term_months": 24,
            "payment_frequency": "bi_weekly",
        },
        preferred="monthly",
    )
    loan = create_loan_from_application(db_session, application)
    assert loan.payment_frequency == "bi_weekly"
    assert len(loan.schedule) == 52


def test_a_frequency_free_application_books_monthly(db_session: Session):
    """No stated frequency anywhere -> monthly, which is what those loans are."""
    application = _bookable_app(db_session, decision={"apr_bps": 1299, "term_months": 24})
    loan = create_loan_from_application(db_session, application)
    assert loan.payment_frequency == "monthly"
    assert len(loan.schedule) == 24


def test_an_unparseable_stored_frequency_is_REFUSED_not_downgraded(db_session: Session):
    """Fail-closed. Booking a monthly loan for a borrower who signed something
    else is precisely the bug this workstream removes, so a frequency the engine
    cannot understand stops the booking instead of silently defaulting."""
    application = _bookable_app(
        db_session,
        decision={"apr_bps": 1299, "term_months": 24},
        preferred="fortnightly-ish",
    )
    with pytest.raises(ValueError, match="unsupported payment frequency"):
        create_loan_from_application(db_session, application)


def test_spelling_variants_are_normalised(db_session: Session):
    """Origination accepts "Bi-Weekly"/"biweekly"; booking stores the canonical
    value, which is also what the migration's CHECK constraint permits."""
    application = _bookable_app(
        db_session, decision={"apr_bps": 1299, "term_months": 24}, preferred="Bi-Weekly"
    )
    loan = create_loan_from_application(db_session, application)
    assert loan.payment_frequency == "bi_weekly"


# ===========================================================================
# 3. QUOTE == BOOKED, row for row — the whole point
# ===========================================================================


def _quote_product():
    """A product whose guardrails allow all four frequencies and a wide band."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        code="freq_parity_v1",
        name="Frequency Parity v1",
        version=1,
        status="active",
        currency="CAD",
        min_amount_cents=100_000,
        max_amount_cents=5_000_000,
        provinces=None,
        pricing_config={
            "schema_version": 1,
            "interest": {
                "annual_rate_bps": 1299,
                "min_rate_bps": 0,
                "max_rate_bps": 2400,
            },
            "payment_frequencies": ALL_FREQUENCIES,
            "term_min_months": 6,
            "term_max_months": 60,
            "default_term_months": 48,
            "fees": [],
        },
        policy_config=None,
    )


@pytest.mark.parametrize("frequency", ALL_FREQUENCIES)
def test_quote_schedule_equals_booked_schedule_row_for_row(frequency):
    """THE consistency guarantee: for identical inputs, the preliminary schedule
    ``POST /admin/origination/quote`` returns and the schedule the booking engine
    builds are the same rows — same count, same dates, same principal, same
    interest, same payment."""
    amount, term, rate = 1_000_000, 48, 1299
    first = date(2026, 7, 31)

    quote = oc.build_quote(
        _quote_product(),
        oc.SelectionInput(
            amount_cents=amount,
            term_months=term,
            annual_rate_bps=rate,
            frequency=frequency,
            start_date=date(2026, 7, 3),
            first_payment_date=first,
        ),
        as_of=date(2026, 7, 1),
    )
    booked = generate_amortization_schedule(
        amount, rate, term, first, frequency=frequency
    )

    assert len(quote.schedule) == len(booked) == payments_in_term(term, frequency)
    for row, ref in zip(quote.schedule, booked):
        assert row.number == ref.installment_number
        assert row.date == ref.due_date
        assert row.principal_cents == ref.principal_cents
        assert row.interest_cents == ref.interest_cents
        assert row.payment_cents == ref.total_cents  # no fees on this product


@pytest.mark.parametrize("frequency", ALL_FREQUENCIES)
@pytest.mark.parametrize("rate", [0, 999, 1299, 2400])
@pytest.mark.parametrize("term", [6, 12, 48])
def test_quote_equals_booked_across_the_grid(frequency, rate, term):
    """The same equality over a rate x term x frequency grid, INCLUDING 0%.

    Interest-free loans used to diverge: the quote ceiled the instalment
    (front-loading the odd cent) while booking floored it and let the FINAL
    payment absorb the remainder. The quote was moved onto the booking engine's
    convention — booking is the money truth — so 0% deals now match too.
    """
    amount, first = 1_234_567, date(2026, 1, 31)
    n = payments_in_term(term, frequency)
    quote = loan_quote.quote_loan(amount, rate, term, frequency, preview_rows=n)
    booked = generate_amortization_schedule(
        amount, rate, term, first, frequency=frequency
    )

    assert len(quote.schedule_preview) == len(booked) == n
    for row, ref in zip(quote.schedule_preview, booked):
        assert (
            row["principal_cents"],
            row["interest_cents"],
            row["payment_cents"],
        ) == (ref.principal_cents, ref.interest_cents, ref.total_cents)


# ===========================================================================
# 4. Monthly is bit-for-bit unchanged
# ===========================================================================


def test_monthly_is_the_default_and_is_unchanged():
    """Omitting ``frequency`` must produce exactly what it always produced."""
    args = (1_000_000, 1299, 24, date(2026, 3, 31))
    assert generate_amortization_schedule(*args) == generate_amortization_schedule(
        *args, frequency="monthly"
    )


def test_monthly_golden_rows_are_pinned():
    """A pinned monthly schedule: rate/12, EDATE stepping with the month-end
    clamp, final row absorbing the remainder. Any accidental change to the
    generalised engine shows up here."""
    rows = generate_amortization_schedule(1_000_000, 1200, 12, date(2026, 1, 31))

    assert len(rows) == 12
    assert rows[0].interest_cents == 10_000  # 1% of the opening balance
    # EDATE clamping: Jan 31 -> Feb 28 -> Mar 31 (NOT Feb 28 -> Mar 28).
    assert [r.due_date for r in rows[:4]] == [
        date(2026, 1, 31),
        date(2026, 2, 28),
        date(2026, 3, 31),
        date(2026, 4, 30),
    ]
    assert sum(r.principal_cents for r in rows) == 1_000_000


def test_monthly_zero_rate_booking_convention_is_untouched():
    """The 0% booking split stays floor-with-remainder-on-the-last-row. (The
    QUOTE moved onto this convention; booking itself did not move.)"""
    rows = generate_amortization_schedule(1_000_000, 0, 12, date(2026, 3, 31))
    assert rows[0].principal_cents == 83_333
    assert rows[-1].principal_cents == 1_000_000 - 83_333 * 11
    assert all(r.interest_cents == 0 for r in rows)


# ===========================================================================
# 5. Stepping semantics — the CEO's rule, shared not duplicated
# ===========================================================================


def test_the_engine_uses_the_servicing_models_stepping_rule():
    """Due dates come from ``servicing_status.step_due_date``, so the schedule
    and the Account-Due-As-Of projection can never disagree about where an
    installment falls."""
    first = date(2026, 1, 31)
    for frequency in ALL_FREQUENCIES:
        rows = generate_amortization_schedule(
            1_000_000, 1299, 12, first, frequency=frequency
        )
        assert [r.due_date for r in rows] == [
            ss.step_due_date(first, i, frequency) for i in range(len(rows))
        ]


def test_semi_monthly_alternates_month_step_and_plus_fifteen_days():
    """Semi-Monthly: ``EDATE(n//2)``, plus 15 days on the odd steps."""
    rows = generate_amortization_schedule(
        1_000_000, 1299, 3, date(2026, 1, 31), frequency="semi_monthly"
    )
    assert len(rows) == 6, "3 months semi-monthly == 6 installments"
    assert [r.due_date for r in rows] == [
        date(2026, 1, 31),
        date(2026, 2, 15),   # +15d
        date(2026, 2, 28),   # EDATE(+1), day clamped
        date(2026, 3, 15),
        date(2026, 3, 31),   # EDATE(+2)
        date(2026, 4, 15),
    ]


def test_weekly_and_bi_weekly_are_pure_day_steps():
    """Calendar-independent: 7 and 14 days, never a month boundary effect."""
    first = date(2026, 1, 31)
    weekly = generate_amortization_schedule(
        1_000_000, 1299, 12, first, frequency="weekly"
    )
    assert len(weekly) == 52
    assert weekly[1].due_date == first + timedelta(days=7)

    bi = generate_amortization_schedule(1_000_000, 1299, 12, first, frequency="bi_weekly")
    assert len(bi) == 26
    assert bi[1].due_date == first + timedelta(days=14)


def test_actual_360_is_frequency_aware_too():
    """The legacy-LMS day-count generalises: the accrual windows simply get
    shorter as the periods do, and the tie-out invariants are unchanged."""
    rows = generate_amortization_schedule(
        1_000_000,
        1299,
        12,
        date(2026, 7, 31),
        frequency="bi_weekly",
        day_count="actual/360",
    )
    assert len(rows) == 26
    assert {(rows[i + 1].due_date - rows[i].due_date).days for i in range(25)} == {14}
    assert sum(r.principal_cents for r in rows) == 1_000_000
    # First accrual window is ONE PERIOD (14 days), not one month.
    assert rows[0].interest_cents == round(1_000_000 * 0.1299 * 14 / 360)


def test_resolve_frequency_accepts_the_tolerated_spellings():
    assert resolve_frequency(None) is PaymentFrequency.MONTHLY
    assert resolve_frequency("") is PaymentFrequency.MONTHLY
    assert resolve_frequency("bi-weekly") is PaymentFrequency.BI_WEEKLY
    assert resolve_frequency("biweekly") is PaymentFrequency.BI_WEEKLY
    assert resolve_frequency("semi-monthly") is PaymentFrequency.SEMI_MONTHLY
    assert resolve_frequency(PaymentFrequency.WEEKLY) is PaymentFrequency.WEEKLY
    with pytest.raises(ValueError):
        resolve_frequency("daily")


# ===========================================================================
# 6. APR moves with frequency when a per-payment fee exists
# ===========================================================================


def test_apr_rises_with_payment_frequency_when_a_per_payment_fee_exists():
    """A $1/payment administration fee is charged 48 times monthly and 104 times
    bi-weekly on the same 48-month advance, so the disclosed Cost of Borrowing
    APR (SOR/2001-104 s.3-4) must be higher bi-weekly. Booking now computes its
    s.347 guard on the REAL frequency, so a deal that only breaches the cap
    bi-weekly is caught bi-weekly."""
    from app.schemas.pricing_config import parse_pricing_config, quote_fees_cents

    cfg = parse_pricing_config(
        {
            "schema_version": 1,
            "interest": {"annual_rate_bps": 1299},
            "payment_frequencies": ALL_FREQUENCIES,
            "fees": [
                {
                    "fee_type": "administration",
                    "calc": "fixed_cents",
                    "amount": 100,
                    "charge_timing": "per_payment",
                }
            ],
        }
    )

    aprs = {}
    for frequency in ALL_FREQUENCIES:
        fees = quote_fees_cents(cfg, CEO_PRINCIPAL_CENTS, CEO_TERM_MONTHS, frequency)
        aprs[frequency] = loan_quote.compute_apr_bps(
            CEO_PRINCIPAL_CENTS, CEO_RATE_BPS, CEO_TERM_MONTHS, frequency, fees
        )

    assert aprs["monthly"] > CEO_RATE_BPS, "a fee lifts the APR above the contract rate"
    assert (
        aprs["monthly"]
        < aprs["semi_monthly"]
        < aprs["bi_weekly"]
        < aprs["weekly"]
    ), f"APR must rise with payment count: {aprs}"


def test_booking_guards_use_the_real_frequency_not_the_literal_monthly(
    db_session: Session,
):
    """Regression on the specific line this workstream fixed: the APR / fee /
    s.347 guards at the booking chokepoint used to be computed on the literal
    string ``"monthly"`` regardless of the deal. Booking a bi-weekly loan whose
    per-payment fees only breach s.347 bi-weekly must now be REFUSED."""
    product = PlatformCreditProduct(
        code=f"freq_guard_{uuid.uuid4().hex[:8]}",
        name="Frequency Guard Probe",
        vertical="dental",
        version=1,
        status="active",
        currency="CAD",
        min_amount_cents=10_000,
        max_amount_cents=5_000_000,
        verification_matrix={},
        decision_ruleset="dental_full_arch_v1.yaml",
        funding_source="payspyre_capital",
        pricing_config={
            "schema_version": 1,
            "interest": {
                "annual_rate_bps": 2400,
                "min_rate_bps": 0,
                "max_rate_bps": 2400,
            },
            "payment_frequencies": ALL_FREQUENCIES,
            "fees": [
                {
                    "fee_type": "administration",
                    "calc": "fixed_cents",
                    # $20 per payment: 48 charges monthly -> 28.08% APR (under the
                    # cap), 208 charges weekly -> 41.90% APR (over it).
                    "amount": 2_000,
                    "charge_timing": "per_payment",
                }
            ],
        },
    )
    db_session.add(product)
    db_session.commit()

    def _app(frequency):
        patient = PlatformPatient(
            email=f"guard-{uuid.uuid4().hex[:8]}@example.com",
            legal_first_name="Ari",
            legal_last_name="Blake",
        )
        db_session.add(patient)
        db_session.commit()
        application = PlatformCreditApplication(
            patient_id=patient.id,
            credit_product_id=product.id,
            credit_product_version=1,
            requested_amount_cents=1_000_000,
            requested_amount_source="clinic",
            status="approved",
            decision={"apr_bps": 2400, "term_months": 48, "payment_frequency": frequency},
        )
        db_session.add(application)
        db_session.commit()
        db_session.refresh(application)
        return application

    # Monthly: $20 x 48 payments — under the cap, books fine.
    monthly_loan = create_loan_from_application(db_session, _app("monthly"))
    assert monthly_loan.payment_frequency == "monthly"

    # Weekly: the SAME $20 charged 208 times — over the s.347 cap, refused.
    with pytest.raises(ValueError, match="Criminal Code"):
        create_loan_from_application(db_session, _app("weekly"))


# ===========================================================================
# 7. servicing_status: stored frequency wins, inference is the legacy fallback
# ===========================================================================


def test_stored_frequency_beats_the_schedule_cadence_inference():
    """The contract term is the authority, not the gaps between rows.

    Schedule surgery can re-date installments, so a bi-weekly loan's plan may not
    LOOK bi-weekly. The stored column keeps it serviced correctly.
    """
    monthly_looking_dates = [date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31)]
    loan = SimpleNamespace(payment_frequency="bi_weekly")
    assert (
        ss._loan_frequency(loan, monthly_looking_dates) is PaymentFrequency.BI_WEEKLY
    )
    # …and the inference on its own would have said monthly.
    assert ss._infer_frequency(monthly_looking_dates) is PaymentFrequency.MONTHLY


def test_legacy_rows_without_a_stored_frequency_still_infer():
    """Every loan booked before migration 082, and every migrated the legacy LMS loan,
    carries no stored cadence and must keep being serviced exactly as before."""
    bi_weekly_dates = [
        date(2026, 7, 31) + timedelta(days=14 * i) for i in range(5)
    ]
    for legacy in (SimpleNamespace(payment_frequency=None), SimpleNamespace()):
        assert ss._loan_frequency(legacy, bi_weekly_dates) is PaymentFrequency.BI_WEEKLY


def test_an_unrecognised_stored_frequency_falls_back_rather_than_500ing():
    """Servicing a live loan must never crash on a bad enum — unlike BOOKING,
    which fails closed. Reading is not the moment to refuse."""
    bi_weekly_dates = [date(2026, 7, 31) + timedelta(days=14 * i) for i in range(5)]
    loan = SimpleNamespace(payment_frequency="nonsense")
    assert ss._loan_frequency(loan, bi_weekly_dates) is PaymentFrequency.BI_WEEKLY


# ===========================================================================
# 8. Hardship deferments append at the LOAN'S interval, not always a month
# ===========================================================================


def test_hardship_deferment_appends_at_the_loans_own_interval(db_session: Session):
    """A deferred installment is re-scheduled AFTER maturity, "one interval
    apart" — and on a bi-weekly loan that interval is 14 days, not a month.

    This matters on the CEO's own example, which contains a deferment: appending
    monthly stretched a bi-weekly deferment months past maturity and inflated the
    estimated extra interest by the same margin.
    """
    from app.services.hardship import (
        HardshipPolicy,
        _validate_and_preview_deferment,
    )

    application = _bookable_app(
        db_session,
        decision={
            "apr_bps": CEO_RATE_BPS,
            "term_months": CEO_TERM_MONTHS,
            "amount_cents": CEO_PRINCIPAL_CENTS,
            "payment_frequency": "bi_weekly",
        },
    )
    loan = create_loan_from_application(
        db_session, application, first_due_date=CEO_FIRST_DUE
    )
    schedule = sorted(loan.schedule, key=lambda s: s.installment_number)
    contract_end = max(s.due_date for s in schedule)

    plan = _validate_and_preview_deferment(
        db_session,
        loan,
        {"installment_ids": [str(s.id) for s in schedule[:2]]},
        HardshipPolicy(),
    )
    appended = [c["new_scheduled_date"] for c in plan["changes"]]

    assert appended == [
        (contract_end + timedelta(days=14)).isoformat(),
        (contract_end + timedelta(days=28)).isoformat(),
    ], "bi-weekly deferments append 14 days apart, not one month apart"
