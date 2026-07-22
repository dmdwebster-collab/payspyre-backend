"""Persistence + read models for the risk-score snapshot (migration 073).

The scoring ENGINE already existed (``scorecards`` + ``flow_engine``); this
module is the missing MEMORY. It writes one immutable row per scoring event and
serves the four surfaces that were blocked on it:

  * the Underwriting / Archive **Risk score** tab (per application);
  * **loans by risk band/segment** (Portfolio report #4);
  * Reports → **Scoring**'s four tables (PSI, accuracy, delinquency, final score);
  * Risks → **Delinquency performance** by risk level.

⚠️ DECISION-PATH. Every write entry point here is called AFTER the decision has
been taken and is wrapped by its caller in a guard: a failure to record the
score must never change, block or roll back a decision. Nothing in this module
is read by ``flow_engine`` or ``flow_orchestrator._decide`` before the outcome
is resolved.

IMMUTABILITY. Rows are append-only (DB trigger, migration 073). ``record_*``
always INSERTs with ``sequence = max+1`` and retires the previous row's
``is_current`` flag — the only mutation the trigger permits.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional
from uuid import UUID

import structlog
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.risk_score import (
    SOURCE_BACKFILL,
    SOURCE_FLOW_ENGINE,
    SOURCE_UNDERWRITER,
    PlatformApplicationRiskScore,
)
from app.models.platform.scorecard import PlatformScorecard
from app.services import risk_scoring as RS

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------


def _next_sequence(db: Session, application_id: UUID) -> int:
    row = (
        db.query(PlatformApplicationRiskScore.sequence)
        .filter(PlatformApplicationRiskScore.application_id == application_id)
        .order_by(PlatformApplicationRiskScore.sequence.desc())
        .first()
    )
    return int(row[0]) + 1 if row else 1


def current_row(
    db: Session, application_id: UUID
) -> Optional[PlatformApplicationRiskScore]:
    return (
        db.query(PlatformApplicationRiskScore)
        .filter(
            PlatformApplicationRiskScore.application_id == application_id,
            PlatformApplicationRiskScore.is_current.is_(True),
        )
        .first()
    )


def history(db: Session, application_id: UUID) -> list[PlatformApplicationRiskScore]:
    return (
        db.query(PlatformApplicationRiskScore)
        .filter(PlatformApplicationRiskScore.application_id == application_id)
        .order_by(PlatformApplicationRiskScore.sequence.asc())
        .all()
    )


def _retire_current(db: Session, application_id: UUID) -> None:
    """Flip ``is_current`` off on the live row (the trigger's one exception)."""
    row = current_row(db, application_id)
    if row is not None:
        row.is_current = False
        db.flush()


def _resolve_scorecard_version(db: Session, scorecard_id: Optional[str]) -> tuple[
    Optional[UUID], Optional[int]
]:
    if not scorecard_id:
        return (None, None)
    try:
        sid = UUID(str(scorecard_id))
    except (TypeError, ValueError):
        return (None, None)
    row = (
        db.query(PlatformScorecard.id, PlatformScorecard.version)
        .filter(PlatformScorecard.id == sid)
        .first()
    )
    if row is None:
        return (sid, None)
    return (row[0], int(row[1]) if row[1] is not None else None)


def _derive(
    db: Session, scorecard_result: Optional[Mapping[str, Any]]
) -> dict[str, Any]:
    """Everything derivable from a scorecard result. Pure except the version lookup."""
    if not scorecard_result:
        return {
            "raw_score": None, "scaled_score": None, "natural_min": None,
            "natural_max": None, "scorecard_id": None, "scorecard_name": None,
            "scorecard_version": None, "risk_band": None, "risk_segment": None,
            "risk_level": None, "probability_of_default": None,
            "odds_good_bad": None, "pd_method": None, "pd_calibration": None,
            "band_credit_limit_cents": None, "band_annual_rate_bps": None,
        }
    raw = scorecard_result.get("total")
    nat_min = scorecard_result.get("natural_min")
    nat_max = scorecard_result.get("natural_max")
    scaled = RS.scale_score(raw, nat_min, nat_max)
    segment = RS.segment_for(scaled)
    cal = RS.calibrate(scaled)
    scorecard_id, version = _resolve_scorecard_version(
        db, scorecard_result.get("scorecard_id")
    )
    band = scorecard_result.get("band")
    return {
        "raw_score": int(raw) if raw is not None else None,
        "scaled_score": scaled,
        "natural_min": int(nat_min) if nat_min is not None else None,
        "natural_max": int(nat_max) if nat_max is not None else None,
        "scorecard_id": scorecard_id,
        "scorecard_name": scorecard_result.get("scorecard_name"),
        "scorecard_version": version,
        "risk_band": band,
        "risk_segment": segment[0] if segment else None,
        "risk_level": RS.risk_level_for(band),
        "probability_of_default": (
            cal.probability_of_default if cal is not None else None
        ),
        "odds_good_bad": cal.odds_good_bad if cal is not None else None,
        "pd_method": RS.PD_METHOD if cal is not None else None,
        "pd_calibration": RS.pd_calibration() if cal is not None else None,
        "band_credit_limit_cents": scorecard_result.get("credit_limit_cents"),
        "band_annual_rate_bps": scorecard_result.get("annual_rate_bps"),
    }


def record_from_flow_decision(
    db: Session,
    application: PlatformCreditApplication,
    flow_decision: Mapping[str, Any],
    *,
    decision_summary: Optional[Mapping[str, Any]] = None,
    scorecard_inputs: Optional[Mapping[str, Any]] = None,
    decided_at: Optional[datetime] = None,
) -> PlatformApplicationRiskScore:
    """Persist the SYSTEM score + decision for an automated decision.

    ``flow_decision`` is ``FlowDecision.to_dict()``. ``decision_summary`` is the
    payload actually written to ``application.decision`` (it may differ from the
    raw flow decision — e.g. a blacklist screen downgrade), and it is that
    payload's outcome we record as the system decision, so the row can never
    disagree with the application row.
    """
    summary = dict(decision_summary or flow_decision)
    scorecard_result = flow_decision.get("scorecard_result")
    derived = _derive(db, scorecard_result)
    decision = summary.get("decision")
    reasons = list(summary.get("decision_reasons") or [])
    now = decided_at or datetime.now(timezone.utc)

    _retire_current(db, application.id)
    row = PlatformApplicationRiskScore(
        application_id=application.id,
        sequence=_next_sequence(db, application.id),
        is_current=True,
        scored_at=now,
        score_source=SOURCE_FLOW_ENGINE,
        is_backfilled=False,
        recommended_decision=decision,
        recommended_decision_text=RS.recommended_decision_text(decision),
        system_decision=decision,
        system_decision_at=now,
        system_decision_actor="flow_engine",
        underwriter_decision=None,
        checks=RS.build_checks(
            scorecard_result=scorecard_result,
            verifications=list(summary.get("verifications_performed") or []),
            decision_reasons=reasons,
            decision=decision,
        ),
        rule_evaluations=RS.build_rule_evaluations(
            scorecard_result=scorecard_result, decision_reasons=reasons
        ),
        scorecard_inputs=dict(scorecard_inputs) if scorecard_inputs else None,
        decision_snapshot=_jsonable(summary),
        notes=_notes_for(scorecard_result),
        **derived,
    )
    db.add(row)
    db.flush()
    logger.info(
        "risk_score_recorded",
        application_id=str(application.id),
        sequence=row.sequence,
        scaled_score=row.scaled_score,
        risk_band=row.risk_band,
        system_decision=row.system_decision,
    )
    return row


def record_underwriter_decision(
    db: Session,
    application: PlatformCreditApplication,
    *,
    outcome: str,
    actor: str,
    reason_codes: Optional[Iterable[str]] = None,
    note: Optional[str] = None,
    decided_at: Optional[datetime] = None,
    is_override: bool = False,
) -> PlatformApplicationRiskScore:
    """Persist a STAFF decision as a new immutable row.

    The score fields, checks and rules are carried forward verbatim from the
    current row (the underwriter decided ON that score — re-deriving them would
    be a different score). When no prior row exists (a file a human decided
    before it was ever auto-scored), the score fields stay NULL and the row is
    still recorded so the overrides reports have both halves of the comparison.
    """
    prior = current_row(db, application.id)
    now = decided_at or datetime.now(timezone.utc)
    reasons = list(reason_codes or [])

    carried: dict[str, Any] = {}
    for field in (
        "raw_score", "scaled_score", "natural_min", "natural_max", "scorecard_id",
        "scorecard_name", "scorecard_version", "risk_band", "risk_segment",
        "risk_level", "probability_of_default", "odds_good_bad", "pd_method",
        "pd_calibration", "band_credit_limit_cents", "band_annual_rate_bps",
        "recommended_decision", "recommended_decision_text",
        "system_decision", "system_decision_at", "system_decision_actor",
        "scorecard_inputs",
    ):
        carried[field] = getattr(prior, field, None)

    notes = list(getattr(prior, "notes", None) or [])
    if prior is None:
        notes.append(
            "No system score existed when this staff decision was recorded; the "
            "score fields are NULL rather than reconstructed."
        )
    if is_override:
        notes.append("Recorded as an override of an existing decision.")

    _retire_current(db, application.id)
    row = PlatformApplicationRiskScore(
        application_id=application.id,
        sequence=_next_sequence(db, application.id),
        is_current=True,
        scored_at=now,
        score_source=SOURCE_UNDERWRITER,
        is_backfilled=False,
        underwriter_decision=outcome,
        underwriter_decision_at=now,
        underwriter_decision_actor=actor,
        checks=list(getattr(prior, "checks", None) or []),
        rule_evaluations=list(getattr(prior, "rule_evaluations", None) or []),
        decision_snapshot=_jsonable(
            {
                "outcome": outcome,
                "reasons": reasons,
                "note": note,
                "by": "admin",
                "override": is_override,
            }
        ),
        notes=notes,
        **carried,
    )
    db.add(row)
    db.flush()
    logger.info(
        "risk_score_underwriter_decision_recorded",
        application_id=str(application.id),
        sequence=row.sequence,
        outcome=outcome,
        system_decision=row.system_decision,
    )
    return row


def _notes_for(scorecard_result: Optional[Mapping[str, Any]]) -> list[str]:
    notes: list[str] = []
    if not scorecard_result:
        notes.append(
            "No scorecard governed this decision (legacy bureau-score cuts); the "
            "numeric score, band, PD and odds are NULL rather than invented."
        )
    else:
        notes.append(
            "probability_of_default / odds_good_bad are a declared PDO calibration "
            "(see pd_calibration), not a measured bad rate."
        )
    return notes


def _jsonable(value: Any) -> Any:
    """Coerce datetimes/UUIDs so a payload survives the JSONB round-trip."""
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


# ---------------------------------------------------------------------------
# Backfill — honest reconstruction, never invention
# ---------------------------------------------------------------------------


def backfill_application(
    db: Session, application: PlatformCreditApplication
) -> Optional[PlatformApplicationRiskScore]:
    """Reconstruct a score row for an already-decided application.

    RULES (the ``closed_at_source`` honesty pattern):

    * only applications that carry a ``decision`` payload are backfilled;
    * anything already having a row is skipped (idempotent);
    * the score/band/PD are reconstructed ONLY when the stored decision carries
      the WS-D ``scorecard`` sub-payload — that is a real recorded computation.
      Otherwise every score field stays NULL. We never re-run the engine against
      today's data to manufacture a historical score;
    * ``decision_by == 'auto'`` → the stored outcome is the SYSTEM decision;
      any other actor → it is the UNDERWRITER decision and the system half stays
      NULL (we cannot know what the machine would have said);
    * ``is_backfilled = true`` and ``score_source = 'backfill'`` on every row, so
      reports can exclude them in one predicate.

    Returns the new row, or ``None`` when the application was skipped.
    """
    # A JSONB column set to Python ``None`` stores the JSON literal ``null``,
    # which is NOT SQL NULL — an undecided application can look "decided" to a
    # naive IS NOT NULL check. Require an actual object.
    if not isinstance(application.decision, Mapping) or not application.decision:
        return None
    if (
        db.query(PlatformApplicationRiskScore.id)
        .filter(PlatformApplicationRiskScore.application_id == application.id)
        .first()
        is not None
    ):
        return None

    decision_payload: Mapping[str, Any] = application.decision or {}
    # The automated path writes {"decision": ...}; the staff path {"outcome": ...}.
    outcome = decision_payload.get("decision") or decision_payload.get("outcome")
    reasons = list(
        decision_payload.get("decision_reasons")
        or decision_payload.get("reasons")
        or []
    )
    scorecard_result = decision_payload.get("scorecard")
    derived = _derive(db, scorecard_result if isinstance(scorecard_result, Mapping) else None)

    actor = application.decision_by
    is_auto = actor == "auto" or actor is None
    when = application.decision_at

    notes = [
        "BACKFILLED from the stored decision payload — not a live scoring run.",
    ]
    if not scorecard_result:
        notes.append(
            "The stored decision carried no scorecard payload, so score, band, "
            "segment, PD and odds are NULL. They are genuinely unknown for this "
            "application and were not reconstructed."
        )
    if not is_auto:
        notes.append(
            "Decided by a human actor; the SYSTEM half of the dual decision is "
            "NULL because no system recommendation was recorded at the time."
        )

    checks = RS.build_checks(
        scorecard_result=scorecard_result if isinstance(scorecard_result, Mapping) else None,
        verifications=list(decision_payload.get("verifications_performed") or []),
        decision_reasons=reasons,
        decision=outcome,
    )

    row = PlatformApplicationRiskScore(
        application_id=application.id,
        sequence=_next_sequence(db, application.id),
        is_current=True,
        scored_at=when or datetime.now(timezone.utc),
        score_source=SOURCE_BACKFILL,
        is_backfilled=True,
        recommended_decision=outcome if is_auto else None,
        recommended_decision_text=(
            RS.recommended_decision_text(outcome) if is_auto else None
        ),
        system_decision=outcome if is_auto else None,
        system_decision_at=when if is_auto else None,
        system_decision_actor="flow_engine" if is_auto else None,
        underwriter_decision=None if is_auto else outcome,
        underwriter_decision_at=None if is_auto else when,
        underwriter_decision_actor=None if is_auto else actor,
        checks=checks,
        rule_evaluations=RS.build_rule_evaluations(
            scorecard_result=scorecard_result
            if isinstance(scorecard_result, Mapping)
            else None,
            decision_reasons=reasons,
        ),
        scorecard_inputs=None,
        decision_snapshot=_jsonable(decision_payload),
        notes=notes,
        **derived,
    )
    db.add(row)
    db.flush()
    return row


def backfill(
    db: Session, *, limit: Optional[int] = None, dry_run: bool = False
) -> dict[str, Any]:
    """Backfill every decided application that has no score row. Idempotent."""
    already_scored = (
        db.query(PlatformApplicationRiskScore.id)
        .filter(
            PlatformApplicationRiskScore.application_id == PlatformCreditApplication.id
        )
        .exists()
    )
    q = (
        db.query(PlatformCreditApplication)
        .filter(PlatformCreditApplication.decision.isnot(None))
        # JSONB ``null`` != SQL NULL: an application whose decision column holds
        # the JSON literal null has not been decided. Require a real object.
        .filter(func.jsonb_typeof(PlatformCreditApplication.decision) == "object")
        .filter(~already_scored)
        .order_by(PlatformCreditApplication.decision_at.asc().nullslast())
    )
    if limit is not None:
        q = q.limit(int(limit))
    candidates = q.all()

    created = 0
    with_score = 0
    for application in candidates:
        if dry_run:
            created += 1
            payload = application.decision or {}
            if payload.get("scorecard"):
                with_score += 1
            continue
        row = backfill_application(db, application)
        if row is not None:
            created += 1
            if row.raw_score is not None:
                with_score += 1
    return {
        "candidates": len(candidates),
        "created": created,
        "with_reconstructed_score": with_score,
        "without_score": created - with_score,
        "dry_run": dry_run,
        "notes": [
            "Backfilled rows carry is_backfilled=true / score_source='backfill'.",
            "Score, band, segment, PD and odds are populated ONLY where the stored "
            "decision recorded a scorecard result; otherwise they stay NULL. No "
            "score is ever re-derived from today's data.",
        ],
    }


# ---------------------------------------------------------------------------
# Read model — the Risk score tab
# ---------------------------------------------------------------------------


def _num(value: Any) -> Optional[float]:
    return float(value) if value is not None else None


def serialize(row: PlatformApplicationRiskScore) -> dict[str, Any]:
    """The Risk-score tab payload (one application, one scoring event)."""
    odds = _num(row.odds_good_bad)
    segment = RS.segment_for(row.scaled_score)
    return {
        "id": str(row.id),
        "application_id": str(row.application_id),
        "sequence": row.sequence,
        "is_current": row.is_current,
        "scored_at": row.scored_at.isoformat() if row.scored_at else None,
        "score_source": row.score_source,
        "is_backfilled": row.is_backfilled,
        "score": {
            "raw": row.raw_score,
            "scaled": row.scaled_score,
            "natural_min": row.natural_min,
            "natural_max": row.natural_max,
            "scale_min": RS.SCALE_MIN,
            "scale_max": RS.SCALE_MAX,
            "gauge_ticks": list(RS.SEGMENT_TICKS),
        },
        "scorecard": {
            "id": str(row.scorecard_id) if row.scorecard_id else None,
            "name": row.scorecard_name,
            "version": row.scorecard_version,
        },
        "risk_band": row.risk_band,
        "risk_segment": row.risk_segment,
        "risk_segment_label": segment[1] if segment else None,
        "risk_level": row.risk_level,
        "probability_of_default": _num(row.probability_of_default),
        "odds_good_bad": odds,
        "odds_display": f"{round(odds):d}:1" if odds is not None else None,
        "pd_method": row.pd_method,
        "pd_calibration": row.pd_calibration,
        "band_credit_limit_cents": row.band_credit_limit_cents,
        "band_annual_rate_bps": row.band_annual_rate_bps,
        "recommended_decision": row.recommended_decision,
        "recommended_decision_text": row.recommended_decision_text,
        "system_decision": {
            "outcome": row.system_decision,
            "at": row.system_decision_at.isoformat() if row.system_decision_at else None,
            "actor": row.system_decision_actor,
        },
        "underwriter_decision": {
            "outcome": row.underwriter_decision,
            "at": (
                row.underwriter_decision_at.isoformat()
                if row.underwriter_decision_at
                else None
            ),
            "actor": row.underwriter_decision_actor,
        },
        "checks": list(row.checks or []),
        "rule_groups": RS.group_rules(list(row.rule_evaluations or [])),
        "rule_evaluations": list(row.rule_evaluations or []),
        "scorecard_inputs": row.scorecard_inputs,
        "decision_snapshot": row.decision_snapshot,
        "notes": list(row.notes or []),
    }


# ---------------------------------------------------------------------------
# Override classification (the Final score / Overrides reports)
# ---------------------------------------------------------------------------

OVERRIDE_NONE = "none"
OVERRIDE_HIGH_SIDE = "high_side"
OVERRIDE_LOW_SIDE = "low_side"


def classify_override(
    system_decision: Optional[str], underwriter_decision: Optional[str]
) -> str:
    """Industry-standard override classification. PURE.

    * **high side** — the underwriter APPROVED what the system would have
      declined (accepting more risk than the scorecard);
    * **low side** — the underwriter DECLINED what the system would have
      approved (taking less risk than the scorecard);
    * otherwise ``none`` — including every case where one of the two halves is
      unknown, which is why the dual decision is persisted separately.

    ``refer``/``manual_review`` on either side is not an override: a referral is
    the system asking for a human, not a disagreement with one.
    """
    if not system_decision or not underwriter_decision:
        return OVERRIDE_NONE
    if system_decision == "declined" and underwriter_decision == "approved":
        return OVERRIDE_HIGH_SIDE
    if system_decision == "approved" and underwriter_decision == "declined":
        return OVERRIDE_LOW_SIDE
    return OVERRIDE_NONE
