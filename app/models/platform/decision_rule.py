"""``platform_decision_rules`` — admin edits over the decision-rules directory.

Mirrors ``platform_notification_rules`` (PR #174): the code registry in
:mod:`app.services.decision_rules` defines which rules EXIST and their shipped
defaults (Turnkey video-08 values + engine defaults); this table stores ONLY the
rows an admin has edited. No row → shipped default applies, so the decision
path is unchanged until someone edits.
"""
from sqlalchemy import Boolean, Column, DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB

from app.db.base import Base


class PlatformDecisionRule(Base):
    __tablename__ = "platform_decision_rules"

    # Rule key, matching app.services.decision_rules.DECISION_RULE_REGISTRY.
    rule_key = Column(String, primary_key=True)

    # NULL-able tri-state is deliberate: an edit row may change only one facet
    # (e.g. just the outcome) and inherit the rest from the registry defaults.
    enabled = Column(Boolean, nullable=True)

    # Partial param overrides, merged key-by-key over the registry defaults
    # (unknown keys ignored). e.g. {"max_percent": 45}.
    params = Column(JSONB, nullable=False, default=dict)

    # approve | manual_review | decline | no_action. Wired rules are
    # outcome-locked at the API layer; NULL inherits the registry default.
    outcome = Column(String, nullable=True)

    updated_by = Column(String, nullable=True)  # staff user id (audit)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"<PlatformDecisionRule(rule_key={self.rule_key!r}, "
            f"enabled={self.enabled}, outcome={self.outcome!r})>"
        )
