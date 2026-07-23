"""Risk-score persistence — DB-backed tests (migration 073).

Proves the parts that only a database can prove:

  * the table is APPEND-ONLY (the trigger rejects UPDATE/DELETE), and a re-score
    or an underwriter decision INSERTS a new row instead of mutating history;
  * exactly one row per application is ``is_current``;
  * the backfill is idempotent, honest (NULL where genuinely unknown) and
    flagged (``is_backfilled`` / ``score_source='backfill'``);
  * the read endpoints return the documented shapes;
  * the four Scoring tables and the loans-by-risk-band aggregation compute.

Run just this file:

    source .venv/bin/activate && \
        python -m pytest tests/test_risk_score_persistence.py -p no:warnings -q
"""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import InternalError, ProgrammingError
from sqlalchemy.orm import Session

from app.api.v1.api import api_router
from app.core.auth import get_current_user
from app.db.base import get_db
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.credit_product import PlatformCreditProduct
from app.models.platform.loan import PlatformLoan
from app.models.platform.patient import PlatformPatient
from app.models.platform.risk_score import PlatformApplicationRiskScore
from app.services import risk_scores

_BASE = "/api/v1/admin/risk-scores"
_ANALYTICS = "/api/v1/admin/analytics"


def _admin():
    return SimpleNamespace(
        id=uuid.uuid4(), roles=[SimpleNamespace(role=SimpleNamespace(name="admin"))]
    )


@pytest.fixture
def client(db_session: Session):
    app = FastAPI()
    app.include_router(api_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = _admin
    yield TestClient(app)
    app.dependency_overrides.clear()


def _product_id(db):
    return (
        db.query(PlatformCreditProduct)
        .filter(PlatformCreditProduct.code == "dental_full_arch_v1")
        .first()
        .id
    )


def _application(db, *, status="approved", decision=None, decision_by=None,
                 decision_at=None, amount=500_000) -> PlatformCreditApplication:
    patient = PlatformPatient(
        email=f"rs-{uuid.uuid4().hex[:8]}@example.com",
        legal_first_name="Risk",
        legal_last_name="Score",
    )
    db.add(patient)
    db.commit()
    db.refresh(patient)
    row = PlatformCreditApplication(
        patient_id=patient.id,
        credit_product_id=_product_id(db),
        credit_product_version=1,
        requested_amount_cents=amount,
        requested_amount_source="clinic",
        status=status,
        flow_state={},
        decision=decision,
        decision_by=decision_by,
        decision_at=decision_at,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _loan(db, application, *, status="active", balance=400_000) -> PlatformLoan:
    loan = PlatformLoan(
        application_id=application.id,
        principal_cents=application.requested_amount_cents,
        annual_rate_bps=1290,
        term_months=24,
        status=status,
        principal_balance_cents=balance,
        currency="CAD",
    )
    db.add(loan)
    db.commit()
    db.refresh(loan)
    return loan


def _flow_decision(decision="approved", total=160, natural=(0, 200)):
    return {
        "decision": decision,
        "decision_reasons": [] if decision == "approved" else ["manual_review_band"],
        "verifications_performed": [
            {"type": "identity", "method": "id_doc_scan", "result": "passed",
             "confidence": 0.99},
            {"type": "bureau", "method": "soft_pull", "result": "passed",
             "confidence": 1.0},
        ],
        "next_state": {"approved": "approved", "declined": "rejected",
                       "manual_review": "under_review"}[decision],
        "events_to_emit": [],
        "scorecard_result": {
            "scorecard_id": None,
            "scorecard_name": "Default",
            "total": total,
            "band": "good",
            "decision": "approved",
            "credit_limit_cents": 1_000_000,
            "annual_rate_bps": 1999,
            "natural_min": natural[0],
            "natural_max": natural[1],
            "breakdown": [
                {"key": "bureau_score", "value": 700, "points": 100,
                 "bin_label": "700+", "missing": False},
                {"key": "dti_ratio", "value": 0.6155, "points": -20,
                 "bin_label": "high", "missing": False},
            ],
        },
    }


# ---------------------------------------------------------------------------
# Write path + immutability
# ---------------------------------------------------------------------------


def test_record_from_flow_decision_persists_the_full_snapshot(db_session):
    app_row = _application(db_session)
    row = risk_scores.record_from_flow_decision(
        db_session, app_row, _flow_decision(), scorecard_inputs={"bureau_score": 700}
    )
    db_session.commit()

    assert row.sequence == 1
    assert row.is_current is True
    assert row.is_backfilled is False
    assert row.raw_score == 160
    assert row.scaled_score == 800            # 160 of 0..200 → 800 of 0..1000
    assert row.risk_band == "good"
    assert row.risk_segment == "target_client"
    assert row.risk_level == "Low"
    assert row.probability_of_default is not None
    assert float(row.odds_good_bad) > 20      # above the 560 anchor
    assert row.pd_method == "pdo_calibration"
    assert row.pd_calibration["anchor_score"] == 560
    assert row.system_decision == "approved"
    assert row.system_decision_actor == "flow_engine"
    assert row.underwriter_decision is None
    assert len(row.checks) == 8
    assert any(
        r["comment"] == "DTI ratio is 61.55% (high)" for r in row.rule_evaluations
    )
    assert row.scorecard_inputs == {"bureau_score": 700}


def test_table_is_append_only(db_session):
    app_row = _application(db_session)
    row = risk_scores.record_from_flow_decision(db_session, app_row, _flow_decision())
    db_session.commit()

    with pytest.raises((InternalError, ProgrammingError)):
        db_session.execute(
            PlatformApplicationRiskScore.__table__.update()
            .where(PlatformApplicationRiskScore.id == row.id)
            .values(raw_score=999)
        )
    db_session.rollback()

    with pytest.raises((InternalError, ProgrammingError)):
        db_session.execute(
            PlatformApplicationRiskScore.__table__.delete().where(
                PlatformApplicationRiskScore.id == row.id
            )
        )
    db_session.rollback()


def test_rescoring_appends_and_never_rewrites_history(db_session):
    app_row = _application(db_session)
    first = risk_scores.record_from_flow_decision(
        db_session, app_row, _flow_decision(total=100)
    )
    db_session.commit()
    first_id, first_score = first.id, first.raw_score

    second = risk_scores.record_from_flow_decision(
        db_session, app_row, _flow_decision(total=180)
    )
    db_session.commit()

    assert second.sequence == 2
    assert second.raw_score == 180
    rows = risk_scores.history(db_session, app_row.id)
    assert [r.sequence for r in rows] == [1, 2]
    original = next(r for r in rows if r.id == first_id)
    assert original.raw_score == first_score      # untouched
    assert original.is_current is False
    assert sum(1 for r in rows if r.is_current) == 1


def test_underwriter_decision_appends_carrying_the_score_forward(db_session):
    app_row = _application(db_session)
    system = risk_scores.record_from_flow_decision(
        db_session, app_row, _flow_decision(decision="manual_review")
    )
    db_session.commit()

    row = risk_scores.record_underwriter_decision(
        db_session, app_row, outcome="approved", actor="user-123",
        reason_codes=[], note="looks fine",
    )
    db_session.commit()

    assert row.sequence == 2
    assert row.score_source == "underwriter"
    # Both halves of the dual decision live on the same row, separately.
    assert row.system_decision == "manual_review"
    assert row.system_decision_actor == "flow_engine"
    assert row.underwriter_decision == "approved"
    assert row.underwriter_decision_actor == "user-123"
    assert row.underwriter_decision_at is not None
    # The score the underwriter actually saw is carried forward verbatim.
    assert row.raw_score == system.raw_score
    assert row.scaled_score == system.scaled_score
    assert row.checks == system.checks


def test_underwriter_decision_without_a_prior_score_is_still_recorded(db_session):
    app_row = _application(db_session, status="under_review")
    row = risk_scores.record_underwriter_decision(
        db_session, app_row, outcome="declined", actor="user-9", reason_codes=["dti"]
    )
    db_session.commit()
    assert row.system_decision is None
    assert row.underwriter_decision == "declined"
    assert row.raw_score is None
    assert any("No system score existed" in n for n in row.notes)


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


def test_backfill_reconstructs_what_it_can_and_nulls_the_rest(db_session):
    when = datetime.now(timezone.utc) - timedelta(days=90)
    # (a) an automated decision with no scorecard payload — score is unknowable.
    plain = _application(
        db_session, status="rejected",
        decision={"decision": "declined", "decision_reasons": ["bureau_below_minimum"]},
        decision_by="auto", decision_at=when,
    )
    # (b) an automated decision that DID record a scorecard result.
    scored = _application(
        db_session, status="approved",
        decision={
            "decision": "approved",
            "decision_reasons": [],
            "scorecard": _flow_decision()["scorecard_result"],
        },
        decision_by="auto", decision_at=when,
    )
    # (c) a staff decision — the SYSTEM half is genuinely unknown.
    staff = _application(
        db_session, status="approved",
        decision={"outcome": "approved", "reasons": [], "by": "admin"},
        decision_by="user-77", decision_at=when,
    )

    result = risk_scores.backfill(db_session)
    db_session.commit()
    assert result["created"] == 3
    assert result["with_reconstructed_score"] == 1

    a = risk_scores.current_row(db_session, plain.id)
    assert a.is_backfilled is True and a.score_source == "backfill"
    assert a.raw_score is None and a.risk_segment is None and a.probability_of_default is None
    assert a.system_decision == "declined"
    assert any("genuinely unknown" in n for n in a.notes)

    b = risk_scores.current_row(db_session, scored.id)
    assert b.raw_score == 160 and b.risk_segment == "target_client"
    assert b.is_backfilled is True

    c = risk_scores.current_row(db_session, staff.id)
    assert c.system_decision is None          # never fabricated
    assert c.underwriter_decision == "approved"
    assert c.underwriter_decision_actor == "user-77"


def test_backfill_is_idempotent_and_skips_undecided(db_session):
    when = datetime.now(timezone.utc)
    _application(db_session, status="approved",
                 decision={"decision": "approved", "decision_reasons": []},
                 decision_by="auto", decision_at=when)
    _application(db_session, status="started")  # no decision → not a candidate

    first = risk_scores.backfill(db_session)
    db_session.commit()
    second = risk_scores.backfill(db_session)
    db_session.commit()
    assert first["created"] == 1
    assert second["candidates"] == 0 and second["created"] == 0


def test_backfill_dry_run_writes_nothing(db_session):
    _application(db_session, status="approved",
                 decision={"decision": "approved", "decision_reasons": []},
                 decision_by="auto", decision_at=datetime.now(timezone.utc))
    result = risk_scores.backfill(db_session, dry_run=True)
    assert result["dry_run"] is True and result["candidates"] == 1
    assert db_session.query(PlatformApplicationRiskScore).count() == 0


# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------


def test_risk_score_tab_endpoint(client, db_session):
    app_row = _application(db_session)
    risk_scores.record_from_flow_decision(db_session, app_row, _flow_decision())
    db_session.commit()

    r = client.get(f"{_BASE}/applications/{app_row.id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_score"] is True
    assert len(body["pipeline_order"]) == 8
    assert len(body["segment_bands"]) == 5
    score = body["score"]
    assert score["score"]["scaled"] == 800
    assert score["score"]["gauge_ticks"] == [0, 142, 219, 460, 560, 1000]
    assert score["risk_segment_label"] == "Target client"
    assert score["odds_display"].endswith(":1")
    assert score["system_decision"]["outcome"] == "approved"
    assert score["underwriter_decision"]["outcome"] is None
    assert [g["group"] for g in score["rule_groups"]]


def test_risk_score_tab_is_honest_when_unscored(client, db_session):
    app_row = _application(db_session, status="started")
    r = client.get(f"{_BASE}/applications/{app_row.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["has_score"] is False and body["score"] is None
    assert body["notes"]


def test_risk_score_tab_404s_for_unknown_application(client):
    r = client.get(f"{_BASE}/applications/{uuid.uuid4()}")
    assert r.status_code == 404


def test_risk_score_history_endpoint(client, db_session):
    app_row = _application(db_session)
    risk_scores.record_from_flow_decision(db_session, app_row, _flow_decision(total=100))
    risk_scores.record_underwriter_decision(
        db_session, app_row, outcome="approved", actor="user-1"
    )
    db_session.commit()
    r = client.get(f"{_BASE}/applications/{app_row.id}/history")
    assert r.status_code == 200
    events = r.json()["events"]
    assert [e["sequence"] for e in events] == [1, 2]
    assert events[1]["underwriter_decision"]["outcome"] == "approved"


def test_backfill_endpoint(client, db_session):
    _application(db_session, status="approved",
                 decision={"decision": "approved", "decision_reasons": []},
                 decision_by="auto", decision_at=datetime.now(timezone.utc))
    r = client.post(f"{_BASE}/backfill", json={"dry_run": True})
    assert r.status_code == 200, r.text
    assert r.json()["candidates"] == 1
    r = client.post(f"{_BASE}/backfill", json={})
    assert r.json()["created"] == 1


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


def test_scoring_report_returns_the_four_tables(client, db_session):
    now = datetime.now(timezone.utc)
    # Baseline population (before the window) + window population, so PSI works.
    for total, days_ago in [(100, 120), (100, 110), (60, 100), (180, 5), (180, 3)]:
        app_row = _application(db_session)
        # ``decided_at`` is the only way to place a row in time — the table is
        # append-only, so scored_at can never be edited afterwards.
        risk_scores.record_from_flow_decision(
            db_session, app_row, _flow_decision(total=total),
            decided_at=now - timedelta(days=days_ago),
        )
        db_session.commit()
        _loan(db_session, app_row)

    r = client.get(f"{_ANALYTICS}/reports/scoring?days=30")
    assert r.status_code == 200, r.text
    body = r.json()
    for key in (
        "system_stability", "scorecard_accuracy", "delinquency_performance", "final_score"
    ):
        assert key in body and body[key] is not None

    stability = body["system_stability"]
    assert len(stability["rows"]) == 5
    assert {"actual_pct", "expected_pct", "a_minus_e", "a_over_e", "ln_a_over_e",
            "psi_index"} <= set(stability["rows"][0])
    assert stability["expected_population"] == 3
    assert stability["actual_population"] == 2
    assert stability["rag"] in (None, "green", "amber", "red")

    accuracy = body["scorecard_accuracy"]["rows"]
    assert len(accuracy) == 5
    assert {"accounts", "bad", "bad_rate_pct", "expected_bad_rate_pct"} <= set(accuracy[0])

    delinq = body["delinquency_performance"]
    assert delinq["columns"] == ["good", "1-30", "31-60", "61-90", "91plus", "written_off"]
    assert delinq["rows"][0]["counts"].keys() == set(delinq["columns"])

    final = body["final_score"]["rows"]
    assert {"applicants", "approved", "approved_pct", "low_side_pct",
            "high_side_pct"} <= set(final[0])


def test_scoring_report_is_honest_with_no_baseline(client, db_session):
    app_row = _application(db_session)
    risk_scores.record_from_flow_decision(db_session, app_row, _flow_decision())
    db_session.commit()
    body = client.get(f"{_ANALYTICS}/reports/scoring?days=30").json()
    stability = body["system_stability"]
    assert stability["total_psi"] is None
    assert any("no expected (baseline) distribution" in n for n in stability["notes"])


def test_final_score_counts_a_low_side_override(client, db_session):
    app_row = _application(db_session)
    risk_scores.record_from_flow_decision(db_session, app_row, _flow_decision("approved"))
    risk_scores.record_underwriter_decision(
        db_session, app_row, outcome="declined", actor="user-5", reason_codes=["policy"]
    )
    db_session.commit()
    rows = client.get(f"{_ANALYTICS}/reports/scoring?days=30").json()["final_score"]["rows"]
    target = next(r for r in rows if r["segment"] == "target_client")
    assert target["applicants"] == 1
    assert target["low_side"] == 1
    assert target["low_side_pct"] == 100.0
    assert target["approved"] == 0


def test_risk_bands_report_and_unscored_residual(client, db_session):
    scored = _application(db_session)
    risk_scores.record_from_flow_decision(db_session, scored, _flow_decision())
    db_session.commit()
    _loan(db_session, scored, balance=300_000)

    unscored = _application(db_session)
    _loan(db_session, unscored, balance=100_000)

    r = client.get(f"{_ANALYTICS}/reports/risk-bands")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["bands"]) == 5
    target = next(b for b in body["bands"] if b["segment"] == "target_client")
    assert target["count"] == 1
    assert target["outstanding_cents"] == 300_000
    assert body["unscored"]["count"] == 1
    assert body["unscored"]["outstanding_cents"] == 100_000


def test_applications_report_risk_ranks_are_no_longer_stubbed(client, db_session):
    app_row = _application(db_session)
    risk_scores.record_from_flow_decision(db_session, app_row, _flow_decision())
    db_session.commit()
    _loan(db_session, app_row)

    body = client.get(f"{_ANALYTICS}/reports/applications").json()
    by_rank = body["by_risk_rank"]
    assert by_rank["ranks"]["Target client"]["count"] == 1
    assert by_rank["unscored"]["count"] == 0
