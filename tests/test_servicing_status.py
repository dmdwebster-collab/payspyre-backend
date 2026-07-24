"""Golden acceptance test for the Account-Due-As-Of / Amount-to-Move layer.

Dave's worked example is the ORACLE (docs/dave_review_2026-07-21/
AMOUNT_TO_MOVE_MODEL.md). Pure, no DB. It first replays the ledger through the
REAL actuals engine (``interest_engine``) to derive the per-row running
outstanding-principal balances and asserts they equal Dave's cached workbook
values — so the numbers fed into ``servicing_status`` are the engine's, not
hand-copied — then drives ``servicing_status`` and asserts every field to the
cent / exact date.

A green test that computes the WRONG number is a failure: if any assertion here
would need loosening, the module is wrong, not the test.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta as _td, timezone

from app.schemas.pricing_config import PaymentFrequency
from app.services import servicing_status as ss
from app.services.interest_engine import LedgerEvent, compute_balances
from app.services.interest_engine import compute_balances as _cb

# --- Dave's inputs ---------------------------------------------------------
PRINCIPAL_CENTS = 1_000_000          # $10,000.00
TERM_MONTHS = 48
FREQUENCY = PaymentFrequency.BI_WEEKLY
ANNUAL_RATE_BPS = 1299               # 12.99%/yr
INSTALLMENT_CENTS = 12_345           # $123.45
MOVE_PCT = 0.50
DISBURSED = date(2026, 7, 17)
FIRST_DUE = date(2026, 7, 31)
AS_OF = date(2026, 10, 9)

# Ledger cash events (date, cash_cents, is_deferment).
LEDGER = [
    (date(2026, 7, 31), 6_000, False),    # Reg. Payment $60.00
    (date(2026, 8, 14), 18_690, False),   # Reg. Payment $186.90
    (date(2026, 8, 25), 0, True),         # Deferment (+1 installment virtual)
    (date(2026, 8, 28), 12_345, False),   # Reg. Payment $123.45
    (date(2026, 9, 11), 12_345, False),   # Reg. Payment $123.45
    (date(2026, 9, 25), 50_000, False),   # Reg. Payment $500.00
    (date(2026, 10, 9), 5_553, False),    # Reg. Payment $55.53
]

# Dave's cached per-row outstanding principal (dollars -> cents).
EXPECTED_OUTSTANDING = [998_982, 985_269, 985_269, 977_833, 970_360, 925_195, 924_252]
# Dave's cached per-row Account Due As Of.
EXPECTED_AODA = [
    date(2026, 7, 31),
    date(2026, 8, 28),
    date(2026, 9, 11),
    date(2026, 9, 25),
    date(2026, 10, 9),
    date(2026, 12, 4),
    date(2026, 12, 18),
]


def _bi_weekly_schedule(first_due: date, count: int) -> list[date]:
    from datetime import timedelta

    return [first_due + timedelta(days=14 * k) for k in range(count)]


def _replay_running_balances() -> list[int]:
    """Replay the ledger through the REAL actuals engine (interest-first,
    ACT/365 from disbursement) and return the outstanding principal after each
    row, at that row's effective date. Deferment rows produce no engine event
    (no cash) but still show a balance (interest accrued, principal unchanged).
    """
    events: list[LedgerEvent] = []
    running: list[int] = []
    for eff, cash, is_deferment in LEDGER:
        if not is_deferment and cash > 0:
            # Interest-first split at the effective date: accrue to date, cover
            # interest, remainder to principal — exactly allocate_regular_payment
            # against the pre-payment balance.
            pre = compute_balances(
                PRINCIPAL_CENTS, ANNUAL_RATE_BPS, DISBURSED, events, eff
            )
            interest_paid = min(cash, pre.interest_due_cents)
            principal_paid = cash - interest_paid
            events.append(
                LedgerEvent(
                    effective_date=eff,
                    principal_paid_cents=principal_paid,
                    interest_paid_cents=interest_paid,
                )
            )
        bal = compute_balances(
            PRINCIPAL_CENTS, ANNUAL_RATE_BPS, DISBURSED, events, eff
        )
        running.append(bal.outstanding_principal_cents)
    return running


def test_running_balances_match_dave_workbook():
    """The engine's per-row outstanding principal reproduces Dave's cached
    workbook values — proving the balances fed into servicing_status are the
    money engine's own."""
    assert _replay_running_balances() == EXPECTED_OUTSTANDING


def _build_status() -> ss.ServicingStatus:
    running = _replay_running_balances()
    schedule = _bi_weekly_schedule(FIRST_DUE, 105)  # 48mo bi-weekly ~ 104 rows
    ledger_rows = [
        ss.ServicingLedgerRow(
            effective_date=eff,
            cash_paid_cents=cash,
            deferment_installments=1 if is_deferment else 0,
            outstanding_principal_cents=out,
        )
        for (eff, cash, is_deferment), out in zip(LEDGER, running)
    ]
    return ss.compute_servicing_status(
        frequency=FREQUENCY,
        installment_cents=INSTALLMENT_CENTS,
        first_due_date=FIRST_DUE,
        move_pct=MOVE_PCT,
        schedule_due_dates=schedule,
        ledger_rows=ledger_rows,
        as_of=AS_OF,
        as_of_outstanding_principal_cents=running[-1],
        as_of_fees_due_cents=0,
        as_of_add_on_balance_cents=0,
    )


def test_golden_summary_fields():
    status = _build_status()
    assert status.outstanding_principal_cents == 924_252, "outstanding principal"
    assert status.account_due_as_of == date(2026, 12, 18), "account due as of"
    assert status.amount_to_move_cents == 12_345, "amount to move ($123.45)"
    assert status.days_past_due == 0, "DPD off Account-Due-As-Of (paid ahead)"
    assert status.paid_to_date_virtual_cents == 117_278, "paid to date virtual"
    assert status.next_scheduled_payment_date == date(2026, 10, 23), "next scheduled"
    assert status.is_paid_in_full is False


def test_golden_per_row_progression():
    status = _build_status()
    assert [r.account_due_as_of for r in status.rows] == EXPECTED_AODA
    assert [r.outstanding_principal_cents for r in status.rows] == EXPECTED_OUTSTANDING


def test_paid_to_date_virtual_includes_deferment_credit():
    """Total cash is $1,049.33; the deferment adds one installment ($123.45) of
    virtual credit -> $1,172.78 with NO cash for it."""
    status = _build_status()
    cash_total = sum(cash for _, cash, is_def in LEDGER if not is_def)
    assert cash_total == 104_933
    assert status.paid_to_date_virtual_cents == cash_total + INSTALLMENT_CENTS


# --- default (strict) move_pct = 1.00 --------------------------------------


def test_default_move_pct_is_strict_full_installment():
    """With the product default (1.00) only a FULL installment moves the account:
    a $60 partial on installment 1 does NOT advance Account-Due-As-Of."""
    schedule = _bi_weekly_schedule(FIRST_DUE, 10)
    rows = [
        ss.ServicingLedgerRow(
            effective_date=FIRST_DUE, cash_paid_cents=6_000, outstanding_principal_cents=994_000
        )
    ]
    status = ss.compute_servicing_status(
        frequency=FREQUENCY,
        installment_cents=INSTALLMENT_CENTS,
        first_due_date=FIRST_DUE,
        move_pct=1.0,
        schedule_due_dates=schedule,
        ledger_rows=rows,
        as_of=FIRST_DUE,
        as_of_outstanding_principal_cents=994_000,
    )
    # $60 < a full installment -> 0 installments moved -> still at first due.
    assert status.account_due_as_of == FIRST_DUE
    assert status.rows[0].installments_moved == 0


def test_move_pct_none_defaults_to_strict():
    assert ss.move_pct_to_bps(None) == ss.DEFAULT_MOVE_BPS == 10_000
    assert ss.move_pct_to_bps(0.5) == 5_000
    assert ss.move_pct_to_bps(1.0) == 10_000


# --- scalar primitive edge checks ------------------------------------------


def test_amount_to_move_uses_round_half_away_from_zero():
    """minimum_required = 11*123.45 - 123.45*0.5 = 1296.225 must ROUND to
    1296.23 (half AWAY from zero); banker's rounding would give the wrong
    1296.22 and a $123.44 amount-to-move."""
    # installments_moved_now = 10, paid_virtual = 117_278.
    assert ss.amount_to_move_cents(10, INSTALLMENT_CENTS, 5_000, 117_278) == 12_345


def test_is_paid_in_full_stays_open_with_fees():
    assert ss.is_paid_in_full(0, 0, 0) is True
    assert ss.is_paid_in_full(0, 500, 0) is False   # open fees keep it OPEN
    assert ss.is_paid_in_full(0, 0, 4_500) is False  # open add-on (NSF) keeps it OPEN
    assert ss.is_paid_in_full(1, 0, 0) is False


def test_days_past_due_none_when_principal_zero():
    assert ss.days_past_due(date(2026, 12, 1), date(2026, 1, 1), 0) is None
    assert ss.days_past_due(date(2026, 12, 1), date(2026, 11, 1), 100) == 30
    assert ss.days_past_due(date(2026, 12, 1), date(2027, 1, 1), 100) == 0  # ahead


def test_semi_monthly_and_monthly_stepping():
    fd = date(2026, 1, 31)
    # Monthly EDATE clamps to month end.
    assert ss.step_due_date(fd, 1, PaymentFrequency.MONTHLY) == date(2026, 2, 28)
    # Semi-monthly: EDATE(+n//2) then +15d when odd.
    assert ss.step_due_date(fd, 1, PaymentFrequency.SEMI_MONTHLY) == date(2026, 2, 15)
    assert ss.step_due_date(fd, 2, PaymentFrequency.SEMI_MONTHLY) == date(2026, 2, 28)


# ===========================================================================
# DB-BACKED RESOLVER glue — build_servicing_status against fakes (no real DB).
# Proves the wiring end-to-end: ledger_view cash mapping, product move_pct
# resolution, and hardship-deferment virtual-credit merge reproduce the golden
# summary. Same in-memory fake idiom as tests/test_loan_ledger.py.
# ===========================================================================


class _FakeTxn:
    _n = 0

    def __init__(self, eff, amount, principal, interest):
        _FakeTxn._n += 1
        self.id = f"txn-{_FakeTxn._n}"
        self.loan_id = "loan-1"
        self.seq = _FakeTxn._n
        self.reference = f"v-loan-1-{self.seq}"
        self.txn_type = "payment"
        self.payment_type = "eft"
        self.repayment_mode = "regular"
        self.amount_cents = amount
        self.principal_cents = principal
        self.interest_cents = interest
        self.fees_cents = 0
        self.add_on_cents = 0
        self.effective_date = eff
        self.processing_date = eff
        self.reverses_transaction_id = None
        self.created_by = "test"
        self.comment = None
        self.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _FakeItem:
    def __init__(self, n, due):
        self.installment_number = n
        self.due_date = due
        self.principal_cents = 10_000
        self.interest_cents = 2_345
        self.total_cents = INSTALLMENT_CENTS
        self.status = "scheduled"
        self.paid_cents = 0


class _FakeLoan:
    def __init__(self, transactions, schedule):
        self.id = "loan-1"
        self.application_id = "app-1"
        self.principal_cents = PRINCIPAL_CENTS
        self.principal_balance_cents = PRINCIPAL_CENTS
        self.annual_rate_bps = ANNUAL_RATE_BPS
        self.status = "active"
        self.disbursed_at = datetime(2026, 7, 17, tzinfo=timezone.utc)
        self.transactions = transactions
        self.schedule = schedule


class _App:
    credit_product_id = "prod-1"


class _Product:
    policy_config = {"amount_to_move_pct": 0.5}


class _Deferment:
    loan_id = "loan-1"
    kind = "deferment"
    status = "active"
    applied_at = datetime(2026, 8, 25, tzinfo=timezone.utc)
    params = {"installment_ids": ["item-x"]}


class _RoutingQuery:
    def __init__(self, first=None, all_=None):
        self._first = first
        self._all = all_ or []

    def filter(self, *a, **k):
        return self

    def first(self):
        return self._first

    def all(self):
        return self._all


class _RoutingSession:
    def query(self, model, *a, **k):
        name = getattr(model, "__name__", str(model))
        if name == "PlatformCreditApplication":
            return _RoutingQuery(first=_App())
        if name == "PlatformCreditProduct":
            return _RoutingQuery(first=_Product())
        if name == "PlatformHardshipRequest":
            return _RoutingQuery(all_=[_Deferment()])
        return _RoutingQuery()


def _golden_transactions():
    """Build ledger payment rows with the engine's interest-first split (what
    record_payment would persist). Deferment has no ledger row (it is a hardship
    record, merged by the resolver)."""
    events = []
    txns = []
    for eff, cash, is_def in LEDGER:
        if is_def:
            continue
        pre = _cb(PRINCIPAL_CENTS, ANNUAL_RATE_BPS, DISBURSED, events, eff)
        interest = min(cash, pre.interest_due_cents)
        principal = cash - interest
        events.append(
            LedgerEvent(effective_date=eff, principal_paid_cents=principal, interest_paid_cents=interest)
        )
        txns.append(_FakeTxn(eff, cash, principal, interest))
    return txns


def test_build_servicing_status_glue_reproduces_golden():
    txns = _golden_transactions()
    schedule = [_FakeItem(k + 1, FIRST_DUE + _td(days=14 * k)) for k in range(105)]
    loan = _FakeLoan(txns, schedule)
    status = ss.build_servicing_status(_RoutingSession(), loan, AS_OF)

    assert status is not None
    assert status.move_bps == 5_000, "product amount_to_move_pct=0.5 resolved"
    assert status.paid_to_date_virtual_cents == 117_278, "cash + 1 deferment credit"
    assert status.account_due_as_of == date(2026, 12, 18)
    assert status.amount_to_move_cents == 12_345
    assert status.days_past_due == 0
    assert status.outstanding_principal_cents == 924_252
    assert status.next_scheduled_payment_date == date(2026, 10, 23)
    assert status.is_paid_in_full is False
    # The deferment lands in-sequence at 2026-08-25 (between the $186.90 and the
    # first $123.45), advancing the account to 2026-09-11 on that row.
    aoda_by_date = {r.effective_date: r.account_due_as_of for r in status.rows}
    assert aoda_by_date[date(2026, 8, 25)] == date(2026, 9, 11)


def test_build_servicing_status_none_move_defaults_when_no_product():
    """No application/product -> strict default (move_bps 10000)."""
    txns = _golden_transactions()
    schedule = [_FakeItem(k + 1, FIRST_DUE + _td(days=14 * k)) for k in range(105)]
    loan = _FakeLoan(txns, schedule)
    loan.application_id = None

    class _EmptySession:
        def query(self, *a, **k):
            return _RoutingQuery()

    status = ss.build_servicing_status(_EmptySession(), loan, AS_OF)
    assert status.move_bps == ss.DEFAULT_MOVE_BPS == 10_000
