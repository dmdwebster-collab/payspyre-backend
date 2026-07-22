"""Admin read APIs for the persisted risk-score model (migration 073).

Three surfaces:

* ``GET  /admin/risk-scores/applications/{id}``          — the Risk score TAB
* ``GET  /admin/risk-scores/applications/{id}/history``  — every scoring event
* ``POST /admin/risk-scores/backfill``                   — the documented,
  idempotent reconstruction pass for applications decided before the model
  existed (never invents a score; see ``risk_scores.backfill``).

The loans-by-risk-band aggregation and the Scoring section's four tables live
with the other reports, under ``/admin/analytics``.

Read-only except the backfill; admin/staff gated at the router level (the
backfill additionally requires the admin role).
"""
from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.auth import require_roles
from app.db.base import get_db
from app.models.platform.credit_application import PlatformCreditApplication
from app.services import risk_scores
from app.services import risk_scoring as RS

router = APIRouter(dependencies=[Depends(require_roles("admin", "staff"))])


class RiskScoreResponse(BaseModel):
    application_id: str
    has_score: bool
    pipeline_order: list[str]
    check_labels: dict[str, str]
    segment_bands: list[dict]
    score: Optional[dict] = None
    notes: list[str] = []


def _segment_bands() -> list[dict]:
    return [
        {"segment": key, "label": label, "min_score": lower}
        for key, label, lower in RS.SEGMENT_BANDS
    ]


@router.get("/applications/{application_id}", response_model=RiskScoreResponse)
def get_risk_score(
    application_id: UUID,
    sequence: Optional[int] = Query(
        None, description="A specific scoring event; default = the current one."
    ),
    db: Session = Depends(get_db),
):
    """The Risk-score tab for one application.

    ``score`` is the full frozen snapshot: numeric score (raw + the 0-1000
    scaled gauge value), scorecard identity + version, band / segment / level,
    probability of default, odds (Good:Bad), the 8-node check pipeline, the
    grouped business-rules tables and BOTH halves of the dual decision. It is
    ``null`` — with ``has_score: false`` — when the application has never been
    scored, rather than a fabricated zero.
    """
    exists = (
        db.query(PlatformCreditApplication.id)
        .filter(PlatformCreditApplication.id == application_id)
        .first()
    )
    if exists is None:
        raise HTTPException(status_code=404, detail="Application not found")

    if sequence is not None:
        rows = [r for r in risk_scores.history(db, application_id) if r.sequence == sequence]
        row = rows[0] if rows else None
        if row is None:
            raise HTTPException(status_code=404, detail="No such scoring event")
    else:
        row = risk_scores.current_row(db, application_id)

    return RiskScoreResponse(
        application_id=str(application_id),
        has_score=row is not None,
        pipeline_order=list(RS.CHECK_ORDER),
        check_labels=dict(RS.CHECK_LABELS),
        segment_bands=_segment_bands(),
        score=risk_scores.serialize(row) if row is not None else None,
        notes=[]
        if row is not None
        else [
            "This application has no risk-score row. It was decided before the "
            "score model existed (run the backfill) or has not been decided yet. "
            "No score is synthesised."
        ],
    )


class RiskScoreHistoryResponse(BaseModel):
    application_id: str
    events: list[dict]


@router.get("/applications/{application_id}/history", response_model=RiskScoreHistoryResponse)
def get_risk_score_history(application_id: UUID, db: Session = Depends(get_db)):
    """Every scoring event for an application, oldest first.

    The table is append-only: a re-score, or an underwriter recording their
    decision, adds a row — it never rewrites one. This endpoint is the audit
    trail behind the Risk-score tab's ``sequence`` selector.
    """
    rows = risk_scores.history(db, application_id)
    return RiskScoreHistoryResponse(
        application_id=str(application_id),
        events=[risk_scores.serialize(r) for r in rows],
    )


class BackfillResponse(BaseModel):
    candidates: int
    created: int
    with_reconstructed_score: int
    without_score: int
    dry_run: bool
    notes: list[str]


@router.post("/backfill", response_model=BackfillResponse)
def backfill_risk_scores(
    payload: dict[str, Any] = Body(default_factory=dict),
    user=Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    """Reconstruct score rows for applications decided before migration 073.

    Idempotent (skips anything that already has a row) and HONEST: the numeric
    score, band, segment, PD and odds are populated only where the stored
    decision payload actually recorded a scorecard result. Everything else stays
    NULL, and every row is stamped ``is_backfilled=true`` /
    ``score_source='backfill'`` so reports can exclude it in one predicate —
    the same provenance discipline as ``closed_at_source``.

    Body: ``{"limit": <int|null>, "dry_run": <bool>}``.
    """
    limit = payload.get("limit")
    dry_run = bool(payload.get("dry_run", False))
    result = risk_scores.backfill(
        db, limit=int(limit) if limit is not None else None, dry_run=dry_run
    )
    if not dry_run:
        db.commit()
    return BackfillResponse(**result)
