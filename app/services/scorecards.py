"""5-band verified-data scorecards (WS-D — Dave mandate #3).

Replaces the 680/600 two-cut with an EDITABLE banded model — but only when a
scorecard is actually configured. The scoring core is PURE (no DB, no network):

* **Additive bin-based scoring.** Each attribute maps a VERIFIED input value
  (post ID + bank + bureau — never self-reported) onto a bin; the bin's points
  (positive OR negative) are summed across attributes. The total has a natural
  min/max (sum of per-attribute bin extremes) and is deliberately NOT clamped.
* **5 bands** (Excellent / Good / Average / Weak / Poor-Fail), each carrying a
  decision outcome (approved / manual_review / declined) plus a per-band credit
  limit and rate. Band = the highest band whose ``min_score`` the total reaches;
  the single floor band (``min_score=None``) catches everything below.
* **Missing input ≠ silent zero-decline.** An attribute whose input is absent
  contributes its ``missing_points`` (default 0) and is flagged in the
  breakdown; the flow engine additionally refuses to treat a band DECLINE as
  final when any required verification is unknown/failed (score only verified
  data — incomplete data routes to a human, never to an auto-decline).

Default behavior guarantee: ``resolve_for_application`` returns ``None`` when no
active scorecard exists (fresh installs seed none), and ``run_flow`` with
``scorecard=None`` is byte-for-byte the legacy path — nothing regresses.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.models.platform.scorecard import PlatformScorecard, PlatformVendorScorecard

# The 5 canonical band names, best → worst (Dave: "excellent, good, average,
# weak and poor. Anything that would be poor would essentially be an automatic
# fail."). Definitions must use exactly these, in this order.
BAND_NAMES: tuple[str, ...] = ("excellent", "good", "average", "weak", "poor")

BandDecision = Literal["approved", "manual_review", "declined"]

# Stable decision_reasons codes contributed by scorecard banding (mirrors the
# flow_engine REASON_* convention; adverse-action wording maps from these).
REASON_SCORECARD_BAND_FAIL = "scorecard_band_fail"
REASON_SCORECARD_BAND_REVIEW = "scorecard_band_review"


class ScoreBin(BaseModel):
    """One scoring bin: ``min_value <= input < max_value`` → ``points``.

    ``min_value=None`` = open below; ``max_value=None`` = open above. Bins are
    half-open on the upper bound so adjacent bins (e.g. 600-660 / 660-720)
    never double-match.
    """

    model_config = ConfigDict(extra="forbid")

    min_value: Optional[float] = None
    max_value: Optional[float] = None
    points: int
    label: Optional[str] = None

    @model_validator(mode="after")
    def _ordered(self) -> "ScoreBin":
        if (
            self.min_value is not None
            and self.max_value is not None
            and self.min_value >= self.max_value
        ):
            raise ValueError(
                f"bin min_value must be < max_value (got {self.min_value} >= {self.max_value})"
            )
        return self

    def contains(self, value: float) -> bool:
        if self.min_value is not None and value < self.min_value:
            return False
        if self.max_value is not None and value >= self.max_value:
            return False
        return True


class ScoreAttribute(BaseModel):
    """One additive scoring attribute keyed to a VERIFIED input.

    ``key`` names an entry of the verified-inputs dict (see
    :func:`build_verified_inputs`), e.g. ``bureau_score``,
    ``monthly_income_cents``, ``nsf_count_90d``, ``micro_lender_txn_count_90d``.
    Boolean inputs score as 1.0 / 0.0.
    """

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1)
    label: Optional[str] = None
    bins: list[ScoreBin] = Field(min_length=1)
    # Points contributed when the input is missing (default 0 — neutral).
    missing_points: int = 0

    @model_validator(mode="after")
    def _no_overlap(self) -> "ScoreAttribute":
        def lo(b: ScoreBin) -> float:
            return b.min_value if b.min_value is not None else float("-inf")

        def hi(b: ScoreBin) -> float:
            return b.max_value if b.max_value is not None else float("inf")

        ordered = sorted(self.bins, key=lambda b: (lo(b), hi(b)))
        for prev, nxt in zip(ordered, ordered[1:]):
            if hi(prev) > lo(nxt):  # half-open bins: touching bounds are fine
                raise ValueError(
                    f"attribute '{self.key}' has overlapping bins "
                    f"([{prev.min_value}, {prev.max_value}) and "
                    f"[{nxt.min_value}, {nxt.max_value}))"
                )
        return self

    def min_points(self) -> int:
        return min(min(b.points for b in self.bins), self.missing_points)

    def max_points(self) -> int:
        return max(max(b.points for b in self.bins), self.missing_points)


class ScoreBand(BaseModel):
    """One of the 5 bands. ``min_score=None`` marks the single floor band."""

    model_config = ConfigDict(extra="forbid")

    name: str
    min_score: Optional[int] = None
    decision: BandDecision
    credit_limit_cents: Optional[int] = Field(default=None, ge=0)
    annual_rate_bps: Optional[int] = Field(default=None, ge=0, le=10_000)


class ScorecardDefinition(BaseModel):
    """The validated shape of ``platform_scorecards.attributes`` + ``bands``."""

    model_config = ConfigDict(extra="forbid")

    attributes: list[ScoreAttribute] = Field(min_length=1)
    bands: list[ScoreBand] = Field(min_length=5, max_length=5)

    @model_validator(mode="after")
    def _bands_valid(self) -> "ScorecardDefinition":
        names = tuple(b.name for b in self.bands)
        if names != BAND_NAMES:
            raise ValueError(
                f"bands must be exactly {list(BAND_NAMES)} in order (got {list(names)})"
            )
        floor = self.bands[-1]
        if floor.min_score is not None:
            raise ValueError("the 'poor' floor band must have min_score=null (catch-all)")
        thresholds = [b.min_score for b in self.bands[:-1]]
        if any(t is None for t in thresholds):
            raise ValueError("every band above 'poor' must set min_score")
        for above, below in zip(thresholds, thresholds[1:]):
            if above <= below:  # type: ignore[operator]
                raise ValueError(
                    "band min_score thresholds must be strictly descending "
                    f"(got {thresholds})"
                )
        # Dave: "poor would essentially be an automatic fail" — but a per-band
        # decision stays editable; we only forbid the floor band APPROVING.
        if floor.decision == "approved":
            raise ValueError("the 'poor' floor band cannot auto-approve")
        return self

    def natural_range(self) -> tuple[int, int]:
        """The (min_total, max_total) the additive model can produce. NO clamping
        happens anywhere — this is informational (admin UI band placement)."""
        return (
            sum(a.min_points() for a in self.attributes),
            sum(a.max_points() for a in self.attributes),
        )

    def band_for(self, total: int) -> ScoreBand:
        for band in self.bands[:-1]:
            if total >= int(band.min_score):  # type: ignore[arg-type]
                return band
        return self.bands[-1]


@dataclass(frozen=True)
class AttributeScore:
    key: str
    value: Optional[float]
    points: int
    bin_label: Optional[str]
    missing: bool


@dataclass(frozen=True)
class ScorecardResult:
    """The pure output of :func:`score` — everything the engine + audit need."""

    scorecard_id: Optional[str]
    scorecard_name: str
    total: int
    band: str
    decision: BandDecision
    credit_limit_cents: Optional[int]
    annual_rate_bps: Optional[int]
    breakdown: list[AttributeScore]
    natural_min: int
    natural_max: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "scorecard_id": self.scorecard_id,
            "scorecard_name": self.scorecard_name,
            "total": self.total,
            "band": self.band,
            "decision": self.decision,
            "credit_limit_cents": self.credit_limit_cents,
            "annual_rate_bps": self.annual_rate_bps,
            "natural_min": self.natural_min,
            "natural_max": self.natural_max,
            "breakdown": [
                {
                    "key": b.key,
                    "value": b.value,
                    "points": b.points,
                    "bin_label": b.bin_label,
                    "missing": b.missing,
                }
                for b in self.breakdown
            ],
        }


@dataclass(frozen=True)
class ScorecardRef:
    """A resolved scorecard handed to the pure engine (id/name + definition)."""

    id: Optional[str]
    name: str
    definition: ScorecardDefinition


def _coerce(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return None


def score(ref: ScorecardRef, inputs: Mapping[str, Any]) -> ScorecardResult:
    """Score a verified-inputs dict against a scorecard. PURE + deterministic.

    Additive: total = Σ matched-bin points (or ``missing_points`` when the input
    is absent / non-numeric). The total is NOT clamped; the band lookup places
    it on the 5-band ladder.
    """
    definition = ref.definition
    breakdown: list[AttributeScore] = []
    total = 0
    for attr in definition.attributes:
        value = _coerce(inputs.get(attr.key))
        if value is None:
            points = attr.missing_points
            breakdown.append(AttributeScore(attr.key, None, points, None, True))
        else:
            matched: Optional[ScoreBin] = next(
                (b for b in attr.bins if b.contains(value)), None
            )
            if matched is None:
                # Value outside every bin (definition gap): neutral like missing —
                # never invent points, and surface it in the breakdown.
                points = attr.missing_points
                breakdown.append(AttributeScore(attr.key, value, points, None, True))
            else:
                points = matched.points
                breakdown.append(
                    AttributeScore(attr.key, value, points, matched.label, False)
                )
        total += points

    band = definition.band_for(total)
    nat_min, nat_max = definition.natural_range()
    return ScorecardResult(
        scorecard_id=ref.id,
        scorecard_name=ref.name,
        total=total,
        band=band.name,
        decision=band.decision,
        credit_limit_cents=band.credit_limit_cents,
        annual_rate_bps=band.annual_rate_bps,
        breakdown=breakdown,
        natural_min=nat_min,
        natural_max=nat_max,
    )


# ---------------------------------------------------------------------------
# Verified inputs — built from the SAME stored verification results the replay
# adapters feed the flow engine (post ID + bank + bureau; never self-reported)
# ---------------------------------------------------------------------------


def build_verified_inputs(stored_results: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """Flatten stored verification payloads into the scorecard input dict.

    Keys (all optional — a missing verification simply yields missing inputs,
    which the engine separately routes to manual review via its unknown/failed
    flags):

    * ``bureau_score``           — hard pull wins over soft (freshest data)
    * ``monthly_income_cents``   — recurring-stream VERIFIED income (Flinks)
    * ``nsf_count_90d``
    * ``account_age_months``
    * ``avg_balance_cents``
    * ``identity_confidence``    — 0.0-1.0 from the KYC vendor
    * ``micro_lender_used`` / ``micro_lender_txn_count_90d`` — statement
      analysis risk attributes (WS-D AI bank-statement analysis)
    * ``monthly_expense_cents`` / ``monthly_free_cash_flow_cents``
    """
    inputs: dict[str, Any] = {}

    bureau = stored_results.get("bureau_hard") or stored_results.get("bureau_soft") or {}
    if bureau.get("credit_score") is not None:
        try:
            inputs["bureau_score"] = int(bureau["credit_score"])
        except (TypeError, ValueError):
            pass

    bank = stored_results.get("bank_link") or {}
    for key in ("monthly_income_cents", "nsf_count_90d", "account_age_months", "avg_balance_cents"):
        if bank.get(key) is not None:
            try:
                inputs[key] = int(bank[key])
            except (TypeError, ValueError):
                continue

    kyc = stored_results.get("kyc_id") or {}
    if kyc.get("confidence") is not None:
        try:
            inputs["identity_confidence"] = float(kyc["confidence"])
        except (TypeError, ValueError):
            pass

    analysis = bank.get("statement_analysis")
    if isinstance(analysis, Mapping):
        risk = analysis.get("risk_attributes")
        if isinstance(risk, Mapping):
            for key in (
                "micro_lender_used",
                "micro_lender_txn_count_90d",
                "monthly_expense_cents",
                "monthly_free_cash_flow_cents",
            ):
                if risk.get(key) is not None:
                    inputs[key] = risk[key]

    return inputs


# ---------------------------------------------------------------------------
# DB resolution — vendor override → platform default → None (legacy two-cut)
# ---------------------------------------------------------------------------


def parse_definition(row: PlatformScorecard) -> ScorecardRef:
    """Validate a DB row's JSONB into a ScorecardRef (raises on bad data)."""
    definition = ScorecardDefinition(
        attributes=row.attributes or [], bands=row.bands or []
    )
    return ScorecardRef(id=str(row.id), name=row.name, definition=definition)


def resolve_for_application(db: Session, application: Any) -> Optional[ScorecardRef]:
    """Resolve the scorecard governing an application, or None (= legacy path).

    Precedence: the application's vendor's assigned scorecard (if ACTIVE) →
    the platform default scorecard (if ACTIVE) → None. A DRAFT or ARCHIVED
    scorecard never scores anything, even while assigned.
    """
    vendor_id: Optional[UUID] = getattr(application, "vendor_id", None)
    row: Optional[PlatformScorecard] = None
    if vendor_id is not None:
        assignment = (
            db.query(PlatformVendorScorecard)
            .filter(PlatformVendorScorecard.vendor_id == vendor_id)
            .first()
        )
        if assignment is not None:
            candidate = (
                db.query(PlatformScorecard)
                .filter(PlatformScorecard.id == assignment.scorecard_id)
                .first()
            )
            if candidate is not None and candidate.status == "active":
                row = candidate
    if row is None:
        row = (
            db.query(PlatformScorecard)
            .filter(
                PlatformScorecard.is_default.is_(True),
                PlatformScorecard.status == "active",
            )
            .first()
        )
    if row is None:
        return None
    return parse_definition(row)
