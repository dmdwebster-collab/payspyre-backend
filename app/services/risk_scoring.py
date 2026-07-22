"""Risk-score derivation — the PURE core behind the persisted score model.

The scoring ENGINE already exists (``app.services.scorecards`` — additive
bin-based, 5 bands, natural min/max) and the flow engine already makes the
decision. What was missing is a *record* of what it computed: Dave's
Underwriting **Risk score** tab, the Reports → **Scoring** section (4 tables),
the Risks **Delinquency performance** chart and the Portfolio **loans grouped by
risk level** report all read off a persisted score, band, probability of
default, odds and risk segment. This module derives every one of those from the
scorecard result, deterministically and without touching the decision.

⚠️ DECISION-PATH NOTE. Nothing in this module feeds back into
``flow_engine.run_flow``. The decision is taken first; these values describe it.
``risk_band`` is the scorecard's OWN band (the thing that decided) and is
reported verbatim; ``risk_segment`` / ``probability_of_default`` / ``odds`` are
DERIVED presentation+reporting values computed after the fact. Changing the
calibration constants below can never change an approve/decline.

--------------------------------------------------------------------------
The three derived quantities, and exactly how honest each one is
--------------------------------------------------------------------------

1. **Scaled score (0..1000)** — our scorecards are additive with a *natural*
   min/max that depends on the configured attributes (e.g. -40..+310), while
   Dave's gauge is drawn on a fixed ``0 · 142 · 219 · 460 · 560 · 1000`` ladder.
   We linearly rescale the raw total onto 0..1000 using the scorecard's own
   natural range and store BOTH numbers. This is a pure change of units — no
   information is invented, and the raw total is always retained.

2. **Risk segment** — Dave's five labels (``Target client`` … ``Rejected
   clients``) read off the SCALED score against his published cuts
   (:data:`SEGMENT_BANDS`). Reporting only.

3. **Probability of default / Odds (Good:Bad)** — a declared, reproducible
   **points-to-double-the-odds (PDO) calibration**, the industry-standard
   scorecard convention:

       odds(score) = ANCHOR_ODDS * 2 ** ((score - ANCHOR_SCORE) / PDO)
       PD          = 1 / (1 + odds)

   The three constants are BUSINESS DEFAULTS, not measurements — they are
   stored on every score row (``pd_calibration``) so any number we ever showed
   can be reproduced and re-based later, and :func:`calibrate` is pure. When a
   real bad-rate history exists the same row shape supports an ``empirical``
   method; until then ``pd_method='pdo_calibration'`` says so on the record.
   **FLAG FOR DAVE:** confirm the anchor (560 → 20:1) and PDO (40). Turnkey
   showed 585 → PD 0.58% / 160:1 and 416 → PD 3.7% / 26:1, which is a much
   steeper curve than any single PDO line can fit; ours is a straight,
   documented approximation, not a reverse-engineering of his vendor's model.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional

# --------------------------------------------------------------------------
# Display scale + Dave's segment ladder (video 05 §A5, video 06 §B4)
# --------------------------------------------------------------------------

SCALE_MIN = 0
SCALE_MAX = 1000

#: (segment key, label, inclusive lower bound on the 0..1000 scaled score).
#: Best → worst; the last entry is the floor.
SEGMENT_BANDS: tuple[tuple[str, str, int], ...] = (
    ("target_client", "Target client", 560),
    ("acceptable_client", "Acceptable client", 460),
    ("restricted_client", "Restricted client", 219),
    ("strongly_restricted_client", "Strongly Restricted client", 142),
    ("rejected_client", "Rejected clients", 0),
)

#: Gauge tick marks Dave draws under every scoring card.
SEGMENT_TICKS: tuple[int, ...] = (0, 142, 219, 460, 560, 1000)

#: Scorecard band (decisioning) → Dave's risk-level word (reporting only).
BAND_RISK_LEVEL: dict[str, str] = {
    "excellent": "Low",
    "good": "Low",
    "average": "Medium",
    "weak": "High",
    "poor": "Very High",
}

# --------------------------------------------------------------------------
# PD calibration — BUSINESS DEFAULTS, flagged for Dave. See module docstring.
# --------------------------------------------------------------------------

PD_ANCHOR_SCORE = 560          # the Target-client cut
PD_ANCHOR_ODDS = 20.0          # good:bad at the anchor
PD_POINTS_TO_DOUBLE_ODDS = 40  # PDO
PD_METHOD = "pdo_calibration"


def pd_calibration() -> dict[str, Any]:
    """The calibration parameters, stored verbatim on every score row."""
    return {
        "method": PD_METHOD,
        "anchor_score": PD_ANCHOR_SCORE,
        "anchor_odds": PD_ANCHOR_ODDS,
        "points_to_double_odds": PD_POINTS_TO_DOUBLE_ODDS,
        "scale_min": SCALE_MIN,
        "scale_max": SCALE_MAX,
    }


@dataclass(frozen=True)
class Calibration:
    """PD + odds for one scaled score."""

    probability_of_default: float   # 0..1
    odds_good_bad: float            # good:bad, e.g. 160.0 → "160:1"

    def odds_display(self) -> str:
        return f"{round(self.odds_good_bad):d}:1"


def calibrate(scaled_score: Optional[float]) -> Optional[Calibration]:
    """PDO calibration. PURE. ``None`` in → ``None`` out (never invent a PD)."""
    if scaled_score is None:
        return None
    exponent = (float(scaled_score) - PD_ANCHOR_SCORE) / float(PD_POINTS_TO_DOUBLE_ODDS)
    # Clamp the exponent so an extreme configured scale cannot overflow; the
    # clamp bounds odds to ~[1e-9, 1e9] which is far outside any display range.
    exponent = max(-30.0, min(30.0, exponent))
    odds = PD_ANCHOR_ODDS * (2.0 ** exponent)
    pd = 1.0 / (1.0 + odds)
    return Calibration(
        probability_of_default=round(pd, 6),
        odds_good_bad=round(odds, 4),
    )


def scale_score(
    raw_total: Optional[int],
    natural_min: Optional[int],
    natural_max: Optional[int],
) -> Optional[int]:
    """Rescale an additive total onto 0..1000 using the scorecard's own natural
    range. PURE. Returns ``None`` when the range is unusable (degenerate or
    unknown) — we never guess a display score.

    Totals outside the natural range (possible only if a definition changed
    between scoring and reporting) are clamped to the scale ends.
    """
    if raw_total is None or natural_min is None or natural_max is None:
        return None
    span = int(natural_max) - int(natural_min)
    if span <= 0:
        return None
    frac = (float(raw_total) - float(natural_min)) / float(span)
    frac = max(0.0, min(1.0, frac))
    return int(round(SCALE_MIN + frac * (SCALE_MAX - SCALE_MIN)))


def segment_for(scaled_score: Optional[float]) -> Optional[tuple[str, str]]:
    """(segment_key, label) for a scaled score, or None. PURE."""
    if scaled_score is None:
        return None
    for key, label, lower in SEGMENT_BANDS:
        if scaled_score >= lower:
            return (key, label)
    return (SEGMENT_BANDS[-1][0], SEGMENT_BANDS[-1][1])


def risk_level_for(band: Optional[str]) -> Optional[str]:
    return BAND_RISK_LEVEL.get(band) if band else None


# --------------------------------------------------------------------------
# The 8-node check pipeline (video 02 §2.5 / video 06 §B4)
# --------------------------------------------------------------------------

CHECK_APPLICATION = "application_check"
CHECK_ANTI_FRAUD = "anti_fraud_check"
CHECK_GEOLOCATION = "geolocation"
CHECK_WEB_ACTIVITY = "web_activity_check"
CHECK_CYBER_SECURITY = "cyber_security_check"
CHECK_CREDIT_BUREAU = "credit_bureau"
CHECK_ID_VERIFICATION = "id_verification"
CHECK_BANK_ACCOUNT = "bank_account_check"

#: Pipeline order — exactly Dave's stepper, left to right.
CHECK_ORDER: tuple[str, ...] = (
    CHECK_APPLICATION,
    CHECK_ANTI_FRAUD,
    CHECK_GEOLOCATION,
    CHECK_WEB_ACTIVITY,
    CHECK_CYBER_SECURITY,
    CHECK_CREDIT_BUREAU,
    CHECK_ID_VERIFICATION,
    CHECK_BANK_ACCOUNT,
)

CHECK_LABELS: dict[str, str] = {
    CHECK_APPLICATION: "Application Check",
    CHECK_ANTI_FRAUD: "Anti-fraud Check",
    CHECK_GEOLOCATION: "Geolocation",
    CHECK_WEB_ACTIVITY: "Web Activity / Behavior Check",
    CHECK_CYBER_SECURITY: "Cyber Security Check",
    CHECK_CREDIT_BUREAU: "Credit Bureau",
    CHECK_ID_VERIFICATION: "ID Verification",
    CHECK_BANK_ACCOUNT: "Bank Account Check",
}

#: Card archetype per check — ``scoring`` draws the arc gauge, ``business_rules``
#: draws a pass/fail count pill.
CHECK_KIND: dict[str, str] = {
    CHECK_APPLICATION: "scoring",
    CHECK_ANTI_FRAUD: "business_rules",
    CHECK_GEOLOCATION: "business_rules",
    CHECK_WEB_ACTIVITY: "business_rules",
    CHECK_CYBER_SECURITY: "business_rules",
    CHECK_CREDIT_BUREAU: "scoring",
    CHECK_ID_VERIFICATION: "business_rules",
    CHECK_BANK_ACCOUNT: "scoring",
}

#: Checks Turnkey runs through third-party vendors PaySpyre does not integrate.
#: They render as an honest ``not_modelled`` node rather than a fake tick.
NOT_MODELLED_CHECKS: frozenset[str] = frozenset(
    {CHECK_WEB_ACTIVITY, CHECK_CYBER_SECURITY}
)

STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_REVIEW = "review"
STATUS_UNKNOWN = "unknown"
STATUS_NOT_RUN = "not_run"
STATUS_NOT_MODELLED = "not_modelled"

#: Which verification ``type`` (flow-engine vocabulary) feeds which check node.
_VERIFICATION_TO_CHECK: dict[str, str] = {
    "identity": CHECK_ID_VERIFICATION,
    "bureau": CHECK_CREDIT_BUREAU,
    "income": CHECK_BANK_ACCOUNT,
}

_VERIFICATION_RESULT_TO_STATUS: dict[str, str] = {
    "passed": STATUS_PASSED,
    "failed": STATUS_FAILED,
    "manual_review": STATUS_REVIEW,
    "unknown": STATUS_UNKNOWN,
}


def _fmt_money_cents(cents: Any) -> str:
    try:
        return f"${int(cents) / 100:,.2f}"
    except (TypeError, ValueError):
        return str(cents)


def _fmt_pct_ratio(value: Any) -> str:
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return str(value)


def _fmt_plain(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


#: Human-readable comment generators, keyed by scorecard attribute key. Dave's
#: business-rules tables read like sentences ("DTI ratio is 61.55%"), so every
#: attribute we know about gets a sentence; unknown keys fall back to a generic
#: but still readable form rather than a raw dict dump.
_ATTRIBUTE_COMMENTS: dict[str, Any] = {
    "bureau_score": lambda v: f"Credit bureau score is {_fmt_plain(v)}",
    "dti_ratio": lambda v: f"DTI ratio is {_fmt_pct_ratio(v)}",
    "monthly_income_cents": lambda v: f"Verified monthly income is {_fmt_money_cents(v)}",
    "monthly_expense_cents": lambda v: f"Average monthly expenses are {_fmt_money_cents(v)}",
    "monthly_free_cash_flow_cents": (
        lambda v: f"Average Monthly Free Cash Flow is {_fmt_money_cents(v)}"
    ),
    "nsf_count_90d": lambda v: f"{_fmt_plain(v)} NSF event(s) in the last 90 days",
    "account_age_months": lambda v: f"Primary bank account is {_fmt_plain(v)} month(s) old",
    "avg_balance_cents": lambda v: f"Average account balance is {_fmt_money_cents(v)}",
    "identity_confidence": lambda v: f"Identity match confidence is {_fmt_pct_ratio(v)}",
    "micro_lender_used": (
        lambda v: "Micro-lender activity detected on the bank statements"
        if v
        else "No micro-lender activity on the bank statements"
    ),
    "micro_lender_txn_count_90d": (
        lambda v: f"{_fmt_plain(v)} micro-lender transaction(s) in the last 90 days"
    ),
}

#: Attribute key → the rules GROUP its row is filed under (Dave's accordions).
GROUP_APPLICATION = "application_check_rules"
GROUP_BANK = "bank_provider_rules"
GROUP_BUREAU = "credit_bureau_rules"
GROUP_IDENTITY = "identity_rules"
GROUP_POLICY = "policy_rules"

GROUP_LABELS: dict[str, str] = {
    GROUP_APPLICATION: "Application check rules",
    GROUP_BANK: "Bank provider rules",
    GROUP_BUREAU: "Credit bureau rules",
    GROUP_IDENTITY: "Identity rules",
    GROUP_POLICY: "Policy rules",
}

_ATTRIBUTE_GROUP: dict[str, str] = {
    "bureau_score": GROUP_BUREAU,
    "dti_ratio": GROUP_APPLICATION,
    "monthly_income_cents": GROUP_BANK,
    "monthly_expense_cents": GROUP_BANK,
    "monthly_free_cash_flow_cents": GROUP_BANK,
    "nsf_count_90d": GROUP_BANK,
    "account_age_months": GROUP_BANK,
    "avg_balance_cents": GROUP_BANK,
    "identity_confidence": GROUP_IDENTITY,
    "micro_lender_used": GROUP_BANK,
    "micro_lender_txn_count_90d": GROUP_BANK,
}

_ATTRIBUTE_LABELS: dict[str, str] = {
    "bureau_score": "Credit bureau score",
    "dti_ratio": "DTI (Debt to Income Ratio)",
    "monthly_income_cents": "Verified monthly income",
    "monthly_expense_cents": "Average monthly expenses",
    "monthly_free_cash_flow_cents": "Average Monthly Free Cash Flow",
    "nsf_count_90d": "NSF events (90 days)",
    "account_age_months": "Bank account age",
    "avg_balance_cents": "Average account balance",
    "identity_confidence": "Identity match confidence",
    "micro_lender_used": "Micro-lender usage",
    "micro_lender_txn_count_90d": "Micro-lender transactions (90 days)",
}


def attribute_label(key: str) -> str:
    return _ATTRIBUTE_LABELS.get(key, key.replace("_", " ").strip().capitalize())


def attribute_group(key: str) -> str:
    return _ATTRIBUTE_GROUP.get(key, GROUP_APPLICATION)


def attribute_comment(key: str, value: Any, missing: bool, bin_label: Optional[str]) -> str:
    """Generate Dave's human-readable rule comment. PURE.

    Missing inputs get an explicit "not available" sentence rather than a
    fabricated value — the same honesty rule the scorecard engine applies when
    it refuses to treat a missing input as a zero.
    """
    if missing or value is None:
        return f"{attribute_label(key)} is not available"
    fmt = _ATTRIBUTE_COMMENTS.get(key)
    sentence = fmt(value) if fmt is not None else f"{attribute_label(key)} is {_fmt_plain(value)}"
    if bin_label:
        sentence = f"{sentence} ({bin_label})"
    return sentence


# --------------------------------------------------------------------------
# Reason-code comments (the non-scorecard half of the rules tables)
# --------------------------------------------------------------------------

#: decision_reasons code → (group, rule name, comment). Reason codes carrying a
#: ``:``-suffix (``verification_failed:identity``) are handled dynamically.
_REASON_RULES: dict[str, tuple[str, str, str]] = {
    "quebec_coming_soon": (
        GROUP_POLICY, "Province eligibility",
        "Quebec is not yet served by this lending programme",
    ),
    "manual_review_band": (
        GROUP_BUREAU, "Score band",
        "The credit score falls inside the manual-review band",
    ),
    "bureau_below_minimum": (
        GROUP_BUREAU, "Minimum credit score",
        "The credit score is below the programme minimum",
    ),
    "active_bankruptcy": (
        GROUP_BUREAU, "Bankruptcy", "An active, undischarged bankruptcy is reported",
    ),
    "bankruptcy_discharge_recent": (
        GROUP_BUREAU, "Bankruptcy discharge",
        "A bankruptcy was discharged more recently than the programme minimum",
    ),
    "fraud_signal_review": (
        GROUP_POLICY, "Anti-fraud signal",
        "The credit bureau returned a high-risk identity signal",
    ),
    "identity_manual_review": (
        GROUP_IDENTITY, "Identity verification",
        "The identity provider referred this applicant for human review",
    ),
    "scorecard_band_fail": (
        GROUP_APPLICATION, "Scorecard band",
        "The scorecard band for this score is a decline band",
    ),
    "scorecard_band_review": (
        GROUP_APPLICATION, "Scorecard band",
        "The scorecard band for this score requires an underwriter review",
    ),
    "blacklist_match": (
        GROUP_POLICY, "Blacklist screen",
        "The applicant matched an entry on the blacklist",
    ),
}


def reason_rule(code: str) -> tuple[str, str, str]:
    """(group, name, comment) for a decision-reason code. PURE, total."""
    if code in _REASON_RULES:
        return _REASON_RULES[code]
    if code.startswith("verification_failed:"):
        subject = code.split(":", 1)[1]
        return (
            GROUP_POLICY,
            f"{subject.replace('_', ' ').capitalize()} verification",
            f"The {subject.replace('_', ' ')} verification failed",
        )
    if code.startswith("verification_unknown:"):
        subject = code.split(":", 1)[1]
        return (
            GROUP_POLICY,
            f"{subject.replace('_', ' ').capitalize()} verification",
            f"The {subject.replace('_', ' ')} verification did not return a result",
        )
    return (GROUP_POLICY, code.replace("_", " ").capitalize(), f"Rule '{code}' matched")


# --------------------------------------------------------------------------
# Recommended-decision prose (Dave's Decision Summary narrative)
# --------------------------------------------------------------------------

_RECOMMENDED_PROSE: dict[str, str] = {
    "approved": (
        "The System recommends approving this loan application. The verified data "
        "scores inside the auto-approve band and no blocking rule matched."
    ),
    "manual_review": (
        "The System recommends additional loan application review by an "
        "Underwriter. To make a final decision on the loan, review the "
        "Creditworthiness details below."
    ),
    "declined": (
        "The System recommends declining this loan application. The verified data "
        "scores below the programme's minimum acceptable band."
    ),
}


def recommended_decision_text(decision: Optional[str]) -> Optional[str]:
    return _RECOMMENDED_PROSE.get(decision or "")


# --------------------------------------------------------------------------
# Building the persisted payloads from a FlowDecision (pure)
# --------------------------------------------------------------------------


def build_checks(
    *,
    scorecard_result: Optional[Mapping[str, Any]],
    verifications: list[Mapping[str, Any]],
    decision_reasons: list[str],
    decision: Optional[str],
) -> list[dict[str, Any]]:
    """Derive the 8-node check pipeline. PURE.

    Every node is emitted, always, in Dave's order — a node we genuinely cannot
    evaluate reports ``not_modelled`` / ``not_run`` rather than a green tick.
    """
    by_check: dict[str, dict[str, Any]] = {}
    for v in verifications:
        node = _VERIFICATION_TO_CHECK.get(str(v.get("type") or ""))
        if node is None:
            continue
        status = _VERIFICATION_RESULT_TO_STATUS.get(str(v.get("result") or ""), STATUS_UNKNOWN)
        entry = by_check.setdefault(node, {"status": status, "methods": []})
        entry["methods"].append(
            {
                "method": v.get("method"),
                "result": v.get("result"),
                "confidence": v.get("confidence"),
            }
        )
        # Worst status wins for the node ring.
        order = [STATUS_PASSED, STATUS_UNKNOWN, STATUS_REVIEW, STATUS_FAILED]
        if order.index(status) > order.index(entry["status"]):
            entry["status"] = status

    fraud = "fraud_signal_review" in decision_reasons or "blacklist_match" in decision_reasons
    quebec = "quebec_coming_soon" in decision_reasons

    out: list[dict[str, Any]] = []
    for key in CHECK_ORDER:
        node: dict[str, Any] = {
            "key": key,
            "label": CHECK_LABELS[key],
            "kind": CHECK_KIND[key],
            "status": STATUS_NOT_RUN,
            "score": None,
            "value": None,
            "points": None,
            "comment": None,
            "detail": {},
        }
        if key in NOT_MODELLED_CHECKS:
            node["status"] = STATUS_NOT_MODELLED
            node["comment"] = (
                "PaySpyre does not integrate a provider for this check; no result "
                "is available."
            )
        elif key == CHECK_APPLICATION:
            if scorecard_result:
                node["status"] = STATUS_PASSED
                node["score"] = scorecard_result.get("total")
                node["points"] = scorecard_result.get("total")
                node["comment"] = (
                    f"Scorecard '{scorecard_result.get('scorecard_name')}' scored "
                    f"{scorecard_result.get('total')} → band "
                    f"'{scorecard_result.get('band')}'"
                )
                node["detail"] = {
                    "scorecard_id": scorecard_result.get("scorecard_id"),
                    "band": scorecard_result.get("band"),
                    "natural_min": scorecard_result.get("natural_min"),
                    "natural_max": scorecard_result.get("natural_max"),
                }
            else:
                node["status"] = STATUS_NOT_RUN
                node["comment"] = (
                    "No scorecard was configured for this application; the legacy "
                    "bureau score cuts decided it."
                )
        elif key == CHECK_ANTI_FRAUD:
            node["status"] = STATUS_REVIEW if fraud else STATUS_PASSED
            node["comment"] = (
                "A fraud or blacklist signal referred this file for review"
                if fraud
                else "No fraud or blacklist signal matched"
            )
        elif key == CHECK_GEOLOCATION:
            node["status"] = STATUS_FAILED if quebec else STATUS_PASSED
            node["comment"] = (
                "The applicant's province is not served by this programme"
                if quebec
                else "The applicant's province is served by this programme"
            )
        else:
            found = by_check.get(key)
            if found is not None:
                node["status"] = found["status"]
                node["detail"] = {"methods": found["methods"]}
                node["comment"] = _check_comment(key, found)
        out.append(node)
    # Decision-level prose lives on the Application Check card (Dave's layout).
    for node in out:
        if node["key"] == CHECK_APPLICATION:
            node["recommended_decision"] = recommended_decision_text(decision)
    return out


def _check_comment(key: str, found: Mapping[str, Any]) -> str:
    label = CHECK_LABELS[key]
    status = found["status"]
    methods = ", ".join(str(m.get("method")) for m in found.get("methods", []) if m.get("method"))
    if status == STATUS_PASSED:
        return f"{label} completed successfully ({methods})" if methods else f"{label} completed"
    if status == STATUS_FAILED:
        return f"{label} failed ({methods})" if methods else f"{label} failed"
    if status == STATUS_REVIEW:
        return f"{label} was referred for human review ({methods})"
    return f"{label} did not return a result ({methods})" if methods else f"{label} did not return a result"


def build_rule_evaluations(
    *,
    scorecard_result: Optional[Mapping[str, Any]],
    decision_reasons: list[str],
) -> list[dict[str, Any]]:
    """Dave's grouped business-rules tables (``Result · Name · Comment``). PURE.

    Two sources, one shape:
      * every scorecard attribute becomes a row whose comment is a generated
        sentence ("DTI ratio is 61.55%") and whose result is pass / review
        (negative contribution) / not_evaluated (missing input);
      * every matched decision-reason code becomes a ``failed`` row in its
        policy group.
    """
    rows: list[dict[str, Any]] = []
    breakdown = (scorecard_result or {}).get("breakdown") or []
    for item in breakdown:
        key = str(item.get("key"))
        missing = bool(item.get("missing"))
        points = item.get("points")
        if missing:
            result = "not_evaluated"
        elif isinstance(points, (int, float)) and points < 0:
            result = "review"
        else:
            result = "passed"
        rows.append(
            {
                "group": attribute_group(key),
                "group_label": GROUP_LABELS[attribute_group(key)],
                "key": key,
                "name": attribute_label(key),
                "result": result,
                "value": item.get("value"),
                "points": points,
                "bin_label": item.get("bin_label"),
                "comment": attribute_comment(
                    key, item.get("value"), missing, item.get("bin_label")
                ),
                "source": "scorecard_attribute",
            }
        )
    for code in decision_reasons:
        group, name, comment = reason_rule(code)
        rows.append(
            {
                "group": group,
                "group_label": GROUP_LABELS[group],
                "key": code,
                "name": name,
                "result": "failed",
                "value": None,
                "points": None,
                "bin_label": None,
                "comment": comment,
                "source": "decision_reason",
            }
        )
    return rows


def group_rules(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Fold flat rule rows into Dave's accordions, in a stable order. PURE."""
    order = [GROUP_APPLICATION, GROUP_BANK, GROUP_BUREAU, GROUP_IDENTITY, GROUP_POLICY]
    grouped: dict[str, list[Mapping[str, Any]]] = {g: [] for g in order}
    for row in rows:
        grouped.setdefault(str(row.get("group") or GROUP_POLICY), []).append(row)
    out = []
    for g in order + [k for k in grouped if k not in order]:
        items = grouped.get(g) or []
        if not items:
            continue
        out.append(
            {
                "group": g,
                "label": GROUP_LABELS.get(g, g),
                "passed": sum(1 for r in items if r.get("result") == "passed"),
                "failed": sum(1 for r in items if r.get("result") == "failed"),
                "review": sum(1 for r in items if r.get("result") == "review"),
                "not_evaluated": sum(1 for r in items if r.get("result") == "not_evaluated"),
                "rules": list(items),
            }
        )
    return out


# --------------------------------------------------------------------------
# Population Stability Index (Reports → Scoring, table 1)
# --------------------------------------------------------------------------

PSI_STABLE_MAX = 0.10
PSI_SHIFT_MAX = 0.25


def psi_row(actual_pct: float, expected_pct: float) -> dict[str, Any]:
    """One PSI row exactly as Dave's table lays it out. PURE.

    ``actual_pct`` / ``expected_pct`` are PERCENTAGES (0..100), matching the
    on-screen columns. Verified against his row 1
    (32.43 / 13.37 → A-E 19.06, A/E 2.43, Ln 0.89, Index 0.1689):

        (A-E)     = actual_pct - expected_pct          [percentage points]
        (A/E)     = actual_pct / expected_pct
        Ln(A/E)   = natural log of the ratio
        Index     = ((A-E) / 100) * Ln(A/E)

    Degenerate cases are SHIPPED VISIBLY, as he ships them: an actual of 0
    yields ``-∞``. JSON cannot carry an infinity, so the numeric fields are
    ``None`` and ``degenerate`` names which infinity it was.
    """
    a = float(actual_pct)
    e = float(expected_pct)
    diff = round(a - e, 4)
    if e == 0.0:
        return {
            "actual_pct": round(a, 4), "expected_pct": round(e, 4),
            "a_minus_e": diff, "a_over_e": None, "ln_a_over_e": None,
            "index": None, "degenerate": "+inf" if a > 0 else None,
        }
    ratio = a / e
    if a == 0.0:
        return {
            "actual_pct": round(a, 4), "expected_pct": round(e, 4),
            "a_minus_e": diff, "a_over_e": 0.0, "ln_a_over_e": None,
            "index": None, "degenerate": "-inf",
        }
    ln = math.log(ratio)
    return {
        "actual_pct": round(a, 4),
        "expected_pct": round(e, 4),
        "a_minus_e": diff,
        "a_over_e": round(ratio, 4),
        "ln_a_over_e": round(ln, 4),
        "index": round((diff / 100.0) * ln, 4),
        "degenerate": None,
    }


def psi_rag(total: Optional[float]) -> Optional[str]:
    """RAG band for the PSI total row (green < 0.10 ≤ amber ≤ 0.25 < red)."""
    if total is None:
        return None
    if total < PSI_STABLE_MAX:
        return "green"
    if total <= PSI_SHIFT_MAX:
        return "amber"
    return "red"
