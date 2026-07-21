"""Per-province compliance engine (Workstream W2 — Turnkey parity, videos 07-08).

The engine that turns the ``platform_province_compliance_rules`` table into
enforcement. Two layers:

* A **pure, DB-free evaluation core** (``evaluate_apr``, ``ProvinceEvaluation``,
  ``RuleView``) that scores a disclosed APR against a single province's rule.
  This is unit-tested without a database.
* A thin **DB CRUD + wiring layer** (``list_rules``, ``get_rule``,
  ``upsert_rule``, ``update_rule``, ``make_pricing_province_check``,
  ``resolve_effective_provinces``, ``seed_placeholder_rules``) that the admin
  API and the product-create gate call.

Blocking model, at product configuration time (see
``app.services.loan_quote.validate_pricing_config``):

* A config whose worst-case APR reaches ``apr_cap_bps`` for ANY targeted,
  enabled province is BLOCKED. Multi-province products therefore clear the
  LOWEST cap automatically (each province is checked independently).
* Crossing ``high_cost_apr_threshold_bps`` blocks only when
  ``high_cost_license_held`` is False (no high-cost licence on file).
* ``license_required`` (e.g. Saskatchewan) and the Quebec-language requirement
  are surfaced as WARNINGS on the evaluation, not APR blocks — they gate
  operations/documents, not the pricing math.

SEED VALUES ARE CONSERVATIVE PLACEHOLDERS. Every seeded row has
``counsel_confirmed = False``; ``list_unconfirmed_rules`` surfaces them so the
admin UI can flag "PENDING COUNSEL — Dave/legal must confirm".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.platform.event import PlatformEvent
from app.models.platform.province_compliance import PlatformProvinceComplianceRule

# Canada's 13 provinces + territories (ISO 3166-2:CA subdivision code → name).
CANADA_PROVINCES: dict[str, str] = {
    "AB": "Alberta",
    "BC": "British Columbia",
    "MB": "Manitoba",
    "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador",
    "NS": "Nova Scotia",
    "NT": "Northwest Territories",
    "NU": "Nunavut",
    "ON": "Ontario",
    "PE": "Prince Edward Island",
    "QC": "Quebec",
    "SK": "Saskatchewan",
    "YT": "Yukon",
}

# Federal Criminal Code s.347 cap (35% APR, in force 2026-01-01). Used ONLY as a
# conservative placeholder ceiling for the per-province apr_cap until counsel
# confirms province-specific maxima — NOT an authoritative provincial number.
_S347_PLACEHOLDER_BPS = 3500

_PENDING_COUNSEL = "PENDING COUNSEL — Dave/legal must confirm this value."


# --------------------------------------------------------------------------- #
# Pure, DB-free evaluation core
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RuleView:
    """A DB-free snapshot of a province rule, so the evaluation core can be
    unit-tested and reused without a Session. Built from an ORM row via
    :meth:`from_orm`, or constructed directly in tests."""

    province_code: str
    province_name: str
    enabled: bool = True
    apr_cap_bps: Optional[int] = None
    high_cost_apr_threshold_bps: Optional[int] = None
    high_cost_license_held: bool = False
    license_required: bool = False
    quebec_language_required: bool = False
    counsel_confirmed: bool = False

    @classmethod
    def from_orm(cls, row: PlatformProvinceComplianceRule) -> "RuleView":
        return cls(
            province_code=row.province_code,
            province_name=row.province_name,
            enabled=bool(row.enabled),
            apr_cap_bps=row.apr_cap_bps,
            high_cost_apr_threshold_bps=row.high_cost_apr_threshold_bps,
            high_cost_license_held=bool(row.high_cost_license_held),
            license_required=bool(row.license_required),
            quebec_language_required=bool(row.quebec_language_required),
            counsel_confirmed=bool(row.counsel_confirmed),
        )


@dataclass(frozen=True)
class ProvinceEvaluation:
    """Result of scoring one APR against one province rule."""

    province_code: str
    ok: bool
    # A hard-block reason (set iff ok is False), suitable for surfacing to the
    # admin who is configuring the product.
    block_reason: Optional[str] = None
    warnings: tuple[str, ...] = field(default_factory=tuple)


def evaluate_apr(rule: RuleView, apr_bps: int) -> ProvinceEvaluation:
    """Score a disclosed APR (bps) against a single province rule. Pure."""
    warnings: list[str] = []

    if rule.license_required:
        warnings.append(
            f"{rule.province_name} requires an alternative-lender licence "
            "regardless of APR — confirm the licence is on file before lending."
        )
    if rule.quebec_language_required:
        warnings.append(
            f"{rule.province_name} mandates French-language loan documentation "
            "(Charter of the French Language) — ensure FR documents are issued."
        )
    if not rule.counsel_confirmed:
        warnings.append(
            f"{rule.province_name} compliance values are UNCONFIRMED placeholders "
            "(counsel_confirmed=False)."
        )

    # Hard APR cap.
    if rule.apr_cap_bps is not None and apr_bps >= rule.apr_cap_bps:
        return ProvinceEvaluation(
            province_code=rule.province_code,
            ok=False,
            block_reason=(
                f"APR {apr_bps / 100:.2f}% reaches the maximum for "
                f"{rule.province_name} ({rule.apr_cap_bps / 100:.2f}%)."
            ),
            warnings=tuple(warnings),
        )

    # High-cost-credit licensing threshold.
    if (
        rule.high_cost_apr_threshold_bps is not None
        and apr_bps >= rule.high_cost_apr_threshold_bps
    ):
        if not rule.high_cost_license_held:
            return ProvinceEvaluation(
                province_code=rule.province_code,
                ok=False,
                block_reason=(
                    f"APR {apr_bps / 100:.2f}% crosses the high-cost-credit "
                    f"licensing threshold for {rule.province_name} "
                    f"({rule.high_cost_apr_threshold_bps / 100:.2f}%) and no "
                    "high-cost licence is recorded (high_cost_license_held=False)."
                ),
                warnings=tuple(warnings),
            )
        warnings.append(
            f"APR {apr_bps / 100:.2f}% is high-cost credit in {rule.province_name}; "
            "high-cost licence obligations apply."
        )

    return ProvinceEvaluation(
        province_code=rule.province_code, ok=True, warnings=tuple(warnings)
    )


# --------------------------------------------------------------------------- #
# DB CRUD
# --------------------------------------------------------------------------- #
def _log_event(db: Session, event_type: str, actor: str, payload: dict) -> None:
    db.add(PlatformEvent(event_type=event_type, actor=actor, payload=payload))


def list_rules(
    db: Session, enabled_only: bool = False
) -> list[PlatformProvinceComplianceRule]:
    """All province rules ordered by code. With ``enabled_only`` restrict to
    provinces PaySpyre currently operates in."""
    query = db.query(PlatformProvinceComplianceRule)
    if enabled_only:
        query = query.filter(PlatformProvinceComplianceRule.enabled.is_(True))
    return query.order_by(PlatformProvinceComplianceRule.province_code).all()


def list_unconfirmed_rules(
    db: Session,
) -> list[PlatformProvinceComplianceRule]:
    """Every rule still on placeholder values (counsel_confirmed=False). The
    admin API surfaces these so nobody treats a placeholder as authoritative."""
    return (
        db.query(PlatformProvinceComplianceRule)
        .filter(PlatformProvinceComplianceRule.counsel_confirmed.is_(False))
        .order_by(PlatformProvinceComplianceRule.province_code)
        .all()
    )


def get_rule(
    db: Session, province_code: str
) -> Optional[PlatformProvinceComplianceRule]:
    return (
        db.query(PlatformProvinceComplianceRule)
        .filter(PlatformProvinceComplianceRule.province_code == province_code.upper())
        .first()
    )


_EDITABLE_FIELDS = (
    "province_name",
    "enabled",
    "apr_cap_bps",
    "high_cost_apr_threshold_bps",
    "high_cost_license_held",
    "license_required",
    "license_notes",
    "comms_window_start_hour",
    "comms_window_end_hour",
    "comms_max_contacts_per_week",
    "required_disclosures",
    "language_requirement",
    "quebec_language_required",
    "counsel_confirmed",
    "notes",
)


def _validate_bps(fields: dict) -> None:
    for key in ("apr_cap_bps", "high_cost_apr_threshold_bps"):
        val = fields.get(key)
        if val is not None and val <= 0:
            raise ValueError(f"{key} must be a positive number of basis points")


def upsert_rule(
    db: Session, province_code: str, fields: dict, actor: str = "system"
) -> PlatformProvinceComplianceRule:
    """Create or replace the rule for a province. ``province_code`` must be one
    of Canada's 13 subdivisions."""
    code = province_code.upper()
    if code not in CANADA_PROVINCES:
        raise ValueError(f"Unknown province code '{province_code}'")
    _validate_bps(fields)

    rule = get_rule(db, code)
    created = rule is None
    if rule is None:
        rule = PlatformProvinceComplianceRule(
            province_code=code,
            province_name=fields.get("province_name", CANADA_PROVINCES[code]),
        )
        db.add(rule)

    for key in _EDITABLE_FIELDS:
        if key in fields and fields[key] is not None:
            setattr(rule, key, fields[key])
    rule.updated_by = actor
    if not created:
        rule.version = (rule.version or 1) + 1

    db.commit()
    db.refresh(rule)
    _log_event(
        db,
        event_type="province_compliance.upserted",
        actor=actor,
        payload={"province_code": code, "created": created},
    )
    db.commit()
    return rule


def update_rule(
    db: Session, province_code: str, fields: dict, actor: str = "system"
) -> PlatformProvinceComplianceRule:
    """Patch an existing rule; raises ValueError if the province has no row."""
    rule = get_rule(db, province_code)
    if rule is None:
        raise ValueError(f"No compliance rule for province '{province_code}'")
    _validate_bps(fields)

    changed: list[str] = []
    for key in _EDITABLE_FIELDS:
        if key in fields:
            setattr(rule, key, fields[key])
            changed.append(key)
    rule.updated_by = actor
    rule.version = (rule.version or 1) + 1

    db.commit()
    db.refresh(rule)
    _log_event(
        db,
        event_type="province_compliance.updated",
        actor=actor,
        payload={"province_code": rule.province_code, "fields_updated": changed},
    )
    db.commit()
    return rule


# --------------------------------------------------------------------------- #
# Product-create wiring
# --------------------------------------------------------------------------- #
def resolve_effective_provinces(
    db: Session, declared: Optional[list[str]]
) -> list[str]:
    """The provinces a product must clear.

    If the product declares provinces, use those (normalized/upper). If it
    declares none, the product can be offered anywhere PaySpyre operates, so it
    must clear EVERY enabled province — the conservative default.
    """
    if declared:
        return [p.upper() for p in declared]
    return [r.province_code for r in list_rules(db, enabled_only=True)]


def make_pricing_province_check(
    db: Session,
) -> Callable[[str, int], Optional[str]]:
    """Build the callable ``loan_quote.validate_pricing_config`` invokes for each
    (province, worst-case APR) pair. Returns a block-reason string on breach, or
    None when the APR is compliant for that province.

    Rules are loaded ONCE up front (small table, ≤13 rows) so the pricing loop
    stays in-memory. An unknown/absent province code yields None — the federal
    s.347 cap enforced elsewhere remains the binding check in that case.
    """
    rules = {r.province_code: RuleView.from_orm(r) for r in list_rules(db)}

    def check(province_code: str, apr_bps: int) -> Optional[str]:
        rule = rules.get((province_code or "").upper())
        if rule is None or not rule.enabled:
            return None
        result = evaluate_apr(rule, apr_bps)
        return None if result.ok else result.block_reason

    return check


# --------------------------------------------------------------------------- #
# Seed data — CONSERVATIVE PLACEHOLDERS, counsel_confirmed=False everywhere
# --------------------------------------------------------------------------- #
def placeholder_seed_rows() -> list[dict]:
    """The placeholder seed set the migration inserts and tests assert against.

    Design of the placeholders (all clearly non-authoritative):

    * ``apr_cap_bps`` = the federal s.347 ceiling (35%) for every province. This
      is the KNOWN binding federal number already in the codebase, used here as
      a safe upper bound — it never claims a province-specific maximum. Counsel
      lowers each province to its true cap.
    * ``high_cost_apr_threshold_bps`` = None (unknown) — we do NOT invent a
      high-cost threshold; counsel supplies each province's figure.
    * Comms window 08:00–21:00 local, ≤3 contacts/week — conservative defaults
      the comms engine can run against, flagged pending counsel.
    * Saskatchewan: ``license_required=True`` (Dave's video note that SK requires
      an alternative-lender licence regardless).
    * Quebec: ``enabled=False`` (preserves the existing Quebec gate) +
      ``quebec_language_required=True`` + French-language requirement.
    """
    rows: list[dict] = []
    for code, name in CANADA_PROVINCES.items():
        rows.append(
            {
                "province_code": code,
                "province_name": name,
                "enabled": code != "QC",  # QC gated off until FR docs land
                "apr_cap_bps": _S347_PLACEHOLDER_BPS,
                "high_cost_apr_threshold_bps": None,
                "high_cost_license_held": False,
                "license_required": code == "SK",
                "license_notes": (
                    "Saskatchewan requires an alternative-lender licence "
                    "regardless of APR. " + _PENDING_COUNSEL
                    if code == "SK"
                    else _PENDING_COUNSEL
                ),
                "comms_window_start_hour": 8,
                "comms_window_end_hour": 21,
                "comms_max_contacts_per_week": 3,
                "required_disclosures": [],
                "language_requirement": "fr-CA" if code == "QC" else None,
                "quebec_language_required": code == "QC",
                "counsel_confirmed": False,
                "notes": _PENDING_COUNSEL,
            }
        )
    return rows


def seed_placeholder_rules(
    db: Session, actor: str = "system"
) -> list[PlatformProvinceComplianceRule]:
    """Idempotently ensure a placeholder row exists for every province. Existing
    rows are left untouched (so counsel-confirmed edits survive re-seeding)."""
    created: list[PlatformProvinceComplianceRule] = []
    for row in placeholder_seed_rows():
        if get_rule(db, row["province_code"]) is None:
            created.append(upsert_rule(db, row["province_code"], row, actor=actor))
    return created


def get_rule_by_id(
    db: Session, rule_id: UUID
) -> Optional[PlatformProvinceComplianceRule]:
    return (
        db.query(PlatformProvinceComplianceRule)
        .filter(PlatformProvinceComplianceRule.id == rule_id)
        .first()
    )
