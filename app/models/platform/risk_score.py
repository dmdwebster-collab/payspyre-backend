"""Immutable, versioned risk-score snapshots (migration 073).

One row per SCORING EVENT on an application. The row is a frozen record of what
the decision engine computed — numeric score, the scorecard version used, the
risk band/segment, probability of default, odds, the 8-node check pipeline, the
grouped business-rule evaluations, and the **dual decision** (system vs
underwriter, each with its own timestamp and actor).

IMMUTABILITY. The table is append-only, enforced by a database trigger
(``platform_application_risk_scores_append_only``) exactly like
``platform_events`` and ``kyc_events``. Re-scoring, or an underwriter recording
their decision, INSERTS a new row with the next ``sequence``; history is never
rewritten. The single exception the trigger permits is flipping ``is_current``
false on a superseded row — the pointer to "the row to show", not a fact about
the score itself.

PROVENANCE. ``score_source`` / ``is_backfilled`` follow the ``closed_at_source``
honesty pattern: a row derived after the fact from a stored decision says so,
and every field it could not derive stays NULL rather than being invented.
"""
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from app.db.base import Base

#: ``score_source`` vocabulary.
SOURCE_FLOW_ENGINE = "flow_engine"      # natively scored by the automated flow
SOURCE_UNDERWRITER = "underwriter"      # a staff decision recorded on the file
SOURCE_BACKFILL = "backfill"            # derived from a pre-existing decision


class PlatformApplicationRiskScore(Base):
    __tablename__ = "platform_application_risk_scores"
    __table_args__ = (
        UniqueConstraint(
            "application_id", "sequence", name="uq_risk_scores_application_sequence"
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    application_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_credit_applications.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: 1-based, monotonic per application. Re-scores append.
    sequence = Column(Integer, nullable=False, default=1)
    #: Exactly one row per application carries ``true`` (partial unique index).
    is_current = Column(Boolean, nullable=False, default=True)

    scored_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    score_source = Column(String, nullable=False, default=SOURCE_FLOW_ENGINE)
    #: Explicit honesty flag — true when the row was reconstructed rather than
    #: computed live. Reports can exclude backfilled rows in one predicate.
    is_backfilled = Column(Boolean, nullable=False, default=False)

    # --- the score itself -------------------------------------------------
    #: Raw additive scorecard total (NOT clamped; may be negative).
    raw_score = Column(Integer, nullable=True)
    #: ``raw_score`` rescaled onto Dave's 0..1000 gauge. NULL when the scorecard
    #: natural range is unusable — never guessed.
    scaled_score = Column(Integer, nullable=True)
    natural_min = Column(Integer, nullable=True)
    natural_max = Column(Integer, nullable=True)

    scorecard_id = Column(
        UUID(as_uuid=True),
        ForeignKey("platform_scorecards.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    scorecard_name = Column(String, nullable=True)
    scorecard_version = Column(Integer, nullable=True)

    #: The scorecard's OWN band — the thing that decided (excellent…poor).
    risk_band = Column(String, nullable=True, index=True)
    #: Dave's reporting label off the 0..1000 ladder (target_client…rejected_client).
    risk_segment = Column(String, nullable=True, index=True)
    #: Dave's "Risk level" word (Low / Medium / High / Very High).
    risk_level = Column(String, nullable=True)

    probability_of_default = Column(Numeric(9, 6), nullable=True)
    odds_good_bad = Column(Numeric(14, 4), nullable=True)
    pd_method = Column(String, nullable=True)
    pd_calibration = Column(JSONB, nullable=True)

    #: Per-band credit limit / rate the scorecard attached to this band.
    band_credit_limit_cents = Column(BigInteger, nullable=True)
    band_annual_rate_bps = Column(Integer, nullable=True)

    recommended_decision = Column(String, nullable=True)
    recommended_decision_text = Column(Text, nullable=True)

    # --- dual decision ----------------------------------------------------
    system_decision = Column(String, nullable=True, index=True)
    system_decision_at = Column(DateTime(timezone=True), nullable=True)
    system_decision_actor = Column(String, nullable=True)

    underwriter_decision = Column(String, nullable=True, index=True)
    underwriter_decision_at = Column(DateTime(timezone=True), nullable=True)
    underwriter_decision_actor = Column(String, nullable=True)

    # --- frozen payloads --------------------------------------------------
    #: The 8-node pipeline: [{key, label, kind, status, score, comment, detail}].
    checks = Column(JSONB, nullable=False, default=list)
    #: Flat rule rows: [{group, name, result, comment, value, points, source}].
    rule_evaluations = Column(JSONB, nullable=False, default=list)
    #: The verified inputs the scorecard consumed (reproducibility).
    scorecard_inputs = Column(JSONB, nullable=True)
    #: Frozen copy of the decision payload as it stood at scoring time.
    decision_snapshot = Column(JSONB, nullable=True)
    #: Free-form provenance / limitation notes carried onto the read API.
    notes = Column(JSONB, nullable=False, default=list)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self) -> str:
        return (
            f"<PlatformApplicationRiskScore(application_id={self.application_id}, "
            f"seq={self.sequence}, score={self.scaled_score}, band={self.risk_band})>"
        )
