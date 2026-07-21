"""WS-D multi-offer approvals — pure validation + fake-session choreography.

No real DB (shared remote test DB is off-limits here). The pure validators are
tested directly; the create/accept choreography runs against a lightweight
in-memory fake session that records added objects and answers the specific
queries ``loan_offers`` issues.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services import loan_offers
from app.services.loan_offers import (
    OfferSpec,
    is_expired,
    validate_offer_count,
    validate_offer_spec,
)

NOW = datetime(2026, 7, 20, tzinfo=timezone.utc)


def make_product(*, min_amount=100_000, max_amount=2_000_000, pricing=None):
    if pricing is None:
        pricing = {
            "schema_version": 1,
            "interest": {"annual_rate_bps": 1499, "min_rate_bps": 999, "max_rate_bps": 2999},
            "term_min_months": 6,
            "term_max_months": 60,
        }
    return SimpleNamespace(
        id=uuid4(),
        min_amount_cents=min_amount,
        max_amount_cents=max_amount,
        pricing_config=pricing,
        currency="CAD",
    )


# --- pure validation -----------------------------------------------------------


def test_valid_offer_passes():
    spec = OfferSpec(amount_cents=500_000, term_months=24, annual_rate_bps=1499)
    assert validate_offer_spec(make_product(), spec) == []


def test_amount_below_product_min_rejected():
    spec = OfferSpec(amount_cents=50_000, term_months=24, annual_rate_bps=1499)
    problems = validate_offer_spec(make_product(), spec)
    assert any("below the product minimum" in p for p in problems)


def test_amount_above_product_max_rejected():
    spec = OfferSpec(amount_cents=9_000_000, term_months=24, annual_rate_bps=1499)
    problems = validate_offer_spec(make_product(), spec)
    assert any("above the product maximum" in p for p in problems)


def test_term_outside_bounds_rejected():
    spec = OfferSpec(amount_cents=500_000, term_months=120, annual_rate_bps=1499)
    problems = validate_offer_spec(make_product(), spec)
    assert any("above the product maximum" in p and "mo" in p for p in problems)


def test_rate_outside_band_rejected():
    spec = OfferSpec(amount_cents=500_000, term_months=24, annual_rate_bps=100)
    problems = validate_offer_spec(make_product(), spec)
    assert any("outside the product band" in p for p in problems)


def test_criminal_rate_rejected():
    spec = OfferSpec(amount_cents=500_000, term_months=24, annual_rate_bps=5000)  # 50% > s.347 35%
    product = make_product(
        pricing={"schema_version": 1,
                 "interest": {"annual_rate_bps": 1499, "min_rate_bps": 999, "max_rate_bps": 9999}}
    )
    problems = validate_offer_spec(product, spec)
    assert any("s.347" in p for p in problems)


def test_first_due_before_start_rejected():
    spec = OfferSpec(amount_cents=500_000, term_months=24, annual_rate_bps=1499,
                     start_date=date(2026, 8, 1), first_due_date=date(2026, 7, 15))
    problems = validate_offer_spec(make_product(), spec)
    assert any("first_due_date must be after start_date" in p for p in problems)


def test_term_options_enforced():
    product = make_product(
        pricing={"schema_version": 1,
                 "interest": {"annual_rate_bps": 1499, "min_rate_bps": 999, "max_rate_bps": 2999},
                 "term_options": [12, 24, 36]}
    )
    ok = validate_offer_spec(product, OfferSpec(amount_cents=500_000, term_months=24, annual_rate_bps=1499))
    assert ok == []
    bad = validate_offer_spec(product, OfferSpec(amount_cents=500_000, term_months=18, annual_rate_bps=1499))
    assert any("term options" in p for p in bad)


def test_validate_offer_count():
    assert validate_offer_count(0, 3, 3) is None
    assert validate_offer_count(1, 3, 3) is not None  # 1 open + 3 new > 3
    assert validate_offer_count(0, 0, 3) is not None  # need at least one
    assert validate_offer_count(2, 1, 3) is None


def test_is_expired():
    open_offer = SimpleNamespace(status="offered", expires_at=NOW - timedelta(days=1))
    assert is_expired(open_offer, NOW) is True
    future = SimpleNamespace(status="offered", expires_at=NOW + timedelta(days=1))
    assert is_expired(future, NOW) is False
    accepted = SimpleNamespace(status="accepted", expires_at=NOW - timedelta(days=1))
    assert is_expired(accepted, NOW) is False  # only 'offered' offers expire
    naive = SimpleNamespace(status="offered", expires_at=datetime(2026, 7, 19))
    assert is_expired(naive, NOW) is True  # naive treated as UTC


# --- fake-session choreography -------------------------------------------------


class FakeQuery:
    def __init__(self, session, entity):
        self.session = session
        self.entity = entity
        self._filters = []

    def filter(self, *args):
        self._filters.append(args)
        return self

    def with_for_update(self, **kwargs):
        return self

    def order_by(self, *args):
        return self

    def all(self):
        return self.session._resolve(self.entity, self._filters)

    def first(self):
        rows = self.session._resolve(self.entity, self._filters)
        return rows[0] if rows else None


class FakeSession:
    """Minimal Session double: records added rows; answers the offers/loan/app
    queries loan_offers.py issues. Not a general ORM — just enough."""

    def __init__(self, *, offers=None, loan_row=None):
        self.added = []
        self.offers = offers or []
        self.loan_row = loan_row  # (id,) tuple or None
        self.committed = False
        self.flushed = False

    # loan_offers builds queries on PlatformLoanOffer / PlatformLoan.id / app
    def query(self, *entities):
        return FakeQuery(self, entities)

    def _resolve(self, entities, _filters):
        from app.models.platform.loan import PlatformLoan
        from app.models.platform.loan_offer import PlatformLoanOffer

        entity = entities[0]
        if entity is PlatformLoanOffer:
            return list(self.offers)
        if entity is PlatformLoan or entity is getattr(PlatformLoan, "id", None):
            return [self.loan_row] if self.loan_row is not None else []
        # PlatformLoanOffer.status column select (list of (status,))
        return [(o.status,) for o in self.offers]

    def add(self, obj):
        self.added.append(obj)

    def flush(self):
        self.flushed = True

    def commit(self):
        self.committed = True


def make_offer(**kwargs):
    defaults = dict(
        id=uuid4(),
        status="offered",
        amount_cents=500_000,
        term_months=24,
        annual_rate_bps=1499,
        first_due_date=None,
        expires_at=NOW + timedelta(days=30),
        accepted_at=None,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def make_application(status="under_review"):
    return SimpleNamespace(
        id=uuid4(),
        patient_id=uuid4(),
        credit_product=make_product(),
        credit_product_id=uuid4(),
        status=status,
        status_updated_at=None,
        decision=None,
        decision_by=None,
        decision_at=None,
        vendor_reprocessing_requested=True,
        flow_state={},
    )


def test_create_offers_sets_approved_and_records_offers(monkeypatch):
    session = FakeSession(offers=[], loan_row=None)
    app = make_application()
    specs = [
        OfferSpec(amount_cents=500_000, term_months=24, annual_rate_bps=1499),
        OfferSpec(amount_cents=700_000, term_months=36, annual_rate_bps=1799),
    ]
    offers = loan_offers.create_offers(session, app, specs, actor="admin-1", now=NOW)
    assert len(offers) == 2
    assert app.status == "approved"
    assert app.decision["mode"] == "multi_offer"
    assert app.vendor_reprocessing_requested is False
    # 2 offers + 1 event were added
    from app.models.platform.loan_offer import PlatformLoanOffer

    added_offers = [a for a in session.added if isinstance(a, PlatformLoanOffer)]
    assert len(added_offers) == 2


def test_create_offers_rejects_when_loan_exists():
    session = FakeSession(offers=[], loan_row=(uuid4(),))
    app = make_application()
    with pytest.raises(loan_offers.OfferError):
        loan_offers.create_offers(
            session, app,
            [OfferSpec(amount_cents=500_000, term_months=24, annual_rate_bps=1499)],
            actor="admin-1", now=NOW,
        )


def test_create_offers_enforces_cap():
    existing = [make_offer(), make_offer(), make_offer()]
    session = FakeSession(offers=existing, loan_row=None)
    app = make_application()
    with pytest.raises(loan_offers.OfferError):
        loan_offers.create_offers(
            session, app,
            [OfferSpec(amount_cents=500_000, term_months=24, annual_rate_bps=1499)],
            actor="admin-1", now=NOW,
        )


def test_create_offers_rejects_bad_status():
    session = FakeSession(offers=[], loan_row=None)
    app = make_application(status="declined")
    with pytest.raises(loan_offers.OfferError):
        loan_offers.create_offers(
            session, app,
            [OfferSpec(amount_cents=500_000, term_months=24, annual_rate_bps=1499)],
            actor="admin-1", now=NOW,
        )


def test_accept_offer_voids_siblings_and_books(monkeypatch):
    picked = make_offer(amount_cents=600_000, term_months=30, annual_rate_bps=1699)
    sibling = make_offer()
    session = FakeSession(offers=[picked, sibling], loan_row=None)
    app = make_application(status="approved")

    booked = SimpleNamespace(id=uuid4(), status="pending_disbursement")

    def fake_book_loan(db, application, first_due_date=None):
        return booked

    import app.services.loan_lifecycle as ll

    monkeypatch.setattr(ll, "book_loan", fake_book_loan)

    offer, loan = loan_offers.accept_offer(
        session, app, picked.id, actor="patient:x", now=NOW
    )
    assert offer.status == "accepted"
    assert sibling.status == "void"
    assert loan is booked
    # accepted terms merged onto the decision so the unchanged booking path uses them
    assert app.decision["amount_cents"] == 600_000
    assert app.decision["apr_bps"] == 1699
    assert app.decision["term_months"] == 30
    assert app.decision["accepted_offer_id"] == str(picked.id)


def test_accept_expired_offer_rejected():
    expired = make_offer(expires_at=NOW - timedelta(days=1))
    session = FakeSession(offers=[expired], loan_row=None)
    app = make_application(status="approved")
    with pytest.raises(loan_offers.OfferError):
        loan_offers.accept_offer(session, app, expired.id, actor="patient:x", now=NOW)
    assert expired.status == "expired"


def test_accept_when_already_accepted_rejected():
    already = make_offer(status="accepted")
    other = make_offer()
    session = FakeSession(offers=[already, other], loan_row=None)
    app = make_application(status="approved")
    with pytest.raises(loan_offers.OfferError):
        loan_offers.accept_offer(session, app, other.id, actor="patient:x", now=NOW)


def test_accept_unknown_offer_rejected():
    session = FakeSession(offers=[make_offer()], loan_row=None)
    app = make_application(status="approved")
    with pytest.raises(loan_offers.OfferError):
        loan_offers.accept_offer(session, app, uuid4(), actor="patient:x", now=NOW)


def test_sweep_expires_overdue_open_offers():
    overdue = make_offer(expires_at=NOW - timedelta(days=1))
    fresh = make_offer(expires_at=NOW + timedelta(days=5))
    session = FakeSession(offers=[overdue, fresh], loan_row=None)
    app = make_application(status="approved")
    flipped = loan_offers.sweep_application_expiry(session, app, now=NOW)
    assert flipped == 1
    assert overdue.status == "expired"
    assert fresh.status == "offered"
