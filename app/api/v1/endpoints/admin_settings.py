"""Admin settings suite (Workstream F — Turnkey Settings parity, videos 07-08).

Four admin-editable surfaces under /api/v1/admin/settings:

* **Decision rules** — the TL "Decision engine > Decision rules" directory:
  per-rule enable toggle, editable thresholds, outcome dropdown. DB rows store
  only edits (registry defaults otherwise); WIRED rules (bureau_score,
  bankruptcy_check) actually steer the flow engine via the overlay in
  ``FlowOrchestrator._decide`` — their outcomes are locked.
* **Company info** — single-row config (legal/operating/brand names, logo +
  favicon refs, multi-contact list) consumed by documents + notifications.
* **Business calendar** — computed Canadian statutory holidays per year +
  province, admin closure/business-day overrides, next-business-day probe.
* **Notification matrix** — the 4-audience × 3-channel × event grid layered on
  the PR #174 rules storage (dashboard cells always-on), plus the attachments
  picker. Per-event subject/body editing stays on the existing
  ``/admin/config/notification-rules`` endpoints (same rows).

Admin-only. Every write is audited via platform_events.
"""
from __future__ import annotations

from datetime import date
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import require_roles
from app.db.base import get_db
from app.models.platform.event import PlatformEvent
from app.services import business_calendar, company_info, decision_rules
from app.services import notification_matrix as matrix_svc

router = APIRouter(dependencies=[Depends(require_roles("admin"))])


def _actor_id(user) -> str:
    return str(getattr(user, "id", "") or "unknown")


def _audit(db: Session, event_type: str, actor: str, payload: dict) -> None:
    db.add(PlatformEvent(event_type=event_type, actor=actor, payload=payload))
    db.flush()


# ---------------------------------------------------------------------------
# Decision rules
# ---------------------------------------------------------------------------


class DecisionRuleRow(BaseModel):
    rule_key: str
    group: str
    name: str
    description: str
    params: dict
    outcome: str
    enabled: bool
    wired: bool
    outcome_locked: bool
    customized: bool
    enforcement: str  # "engine" (wired) | "directory_only" (stored, not yet wired)


class DecisionRuleUpdate(BaseModel):
    enabled: Optional[bool] = None
    params: Optional[dict] = Field(
        default=None,
        description="Partial threshold overrides, merged over registry defaults",
    )
    outcome: Optional[str] = Field(
        default=None, description="approve | manual_review | decline | no_action"
    )


def _rule_to_row(rule: decision_rules.EffectiveRule) -> DecisionRuleRow:
    return DecisionRuleRow(
        rule_key=rule.key,
        group=rule.group,
        name=rule.name,
        description=rule.description,
        params=rule.params,
        outcome=rule.outcome,
        enabled=rule.enabled,
        wired=rule.wired,
        outcome_locked=rule.outcome_locked,
        customized=rule.customized,
        enforcement="engine" if rule.wired else "directory_only",
    )


@router.get("/decision-rules", response_model=list[DecisionRuleRow])
def list_decision_rules(db: Session = Depends(get_db)):
    """The full directory (registry ⊕ edits), in TL screen order."""
    return [_rule_to_row(r) for r in decision_rules.get_effective_rules(db)]


@router.put("/decision-rules/{rule_key}", response_model=DecisionRuleRow)
def update_decision_rule(
    rule_key: str,
    body: DecisionRuleUpdate,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin")),
):
    """Upsert the edit row for one rule. Wired rules: thresholds editable,
    outcome locked. Every change is audited with before/after."""
    from app.models.platform.decision_rule import PlatformDecisionRule

    if rule_key not in decision_rules.DECISION_RULE_REGISTRY:
        raise HTTPException(status_code=404, detail="unknown decision rule")
    error = decision_rules.validate_edit(
        rule_key, params=body.params, outcome=body.outcome
    )
    if error:
        raise HTTPException(status_code=422, detail=error)

    before = _rule_to_row(decision_rules.get_effective_rule(db, rule_key)).model_dump()

    row = db.get(PlatformDecisionRule, rule_key)
    if row is None:
        row = PlatformDecisionRule(rule_key=rule_key, params={})
        db.add(row)
    if body.enabled is not None:
        row.enabled = body.enabled
    if body.params is not None:
        merged = dict(row.params or {})
        merged.update(body.params)
        row.params = merged
    if body.outcome is not None:
        row.outcome = body.outcome
    row.updated_by = _actor_id(user)
    db.flush()

    after = _rule_to_row(decision_rules.get_effective_rule(db, rule_key)).model_dump()
    _audit(
        db,
        "admin_decision_rule_updated",
        _actor_id(user),
        {
            "rule_key": rule_key,
            "before": {k: before[k] for k in ("enabled", "params", "outcome")},
            "after": {k: after[k] for k in ("enabled", "params", "outcome")},
            "wired": before["wired"],
        },
    )
    db.commit()
    return _rule_to_row(decision_rules.get_effective_rule(db, rule_key))


@router.delete("/decision-rules/{rule_key}", response_model=DecisionRuleRow)
def reset_decision_rule(
    rule_key: str,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin")),
):
    """Remove the edit row — the rule reverts to its shipped default."""
    from app.models.platform.decision_rule import PlatformDecisionRule

    if rule_key not in decision_rules.DECISION_RULE_REGISTRY:
        raise HTTPException(status_code=404, detail="unknown decision rule")
    row = db.get(PlatformDecisionRule, rule_key)
    if row is not None:
        db.delete(row)
        _audit(
            db,
            "admin_decision_rule_reset",
            _actor_id(user),
            {"rule_key": rule_key},
        )
        db.commit()
    return _rule_to_row(decision_rules.get_effective_rule(db, rule_key))


# ---------------------------------------------------------------------------
# Company info
# ---------------------------------------------------------------------------


class ContactItem(BaseModel):
    kind: str = Field(pattern="^(phone|email|address|website)$")
    label: str = ""
    value: str
    is_primary: bool = False


class CompanyInfoBody(BaseModel):
    legal_name: Optional[str] = None
    operating_name: Optional[str] = None
    brand_name: Optional[str] = None
    lending_type: Optional[str] = None
    logo_ref: Optional[str] = None
    favicon_ref: Optional[str] = None
    contacts: Optional[list[ContactItem]] = None


class CompanyInfoRow(BaseModel):
    legal_name: str
    operating_name: str
    brand_name: Optional[str]
    lending_type: Optional[str]
    logo_ref: Optional[str]
    favicon_ref: Optional[str]
    contacts: list[dict]
    customized: bool  # a DB row exists (vs shipped defaults)


def _company_row(db: Session) -> CompanyInfoRow:
    from app.models.platform.company_info import PlatformCompanyInfo

    info = company_info.get_company_info(db)
    return CompanyInfoRow(
        legal_name=info.legal_name,
        operating_name=info.operating_name,
        brand_name=info.brand_name,
        lending_type=info.lending_type,
        logo_ref=info.logo_ref,
        favicon_ref=info.favicon_ref,
        contacts=info.contacts,
        customized=db.get(PlatformCompanyInfo, 1) is not None,
    )


@router.get("/company-info", response_model=CompanyInfoRow)
def get_company_info_endpoint(db: Session = Depends(get_db)):
    """Effective company info (DB row 1, else shipped defaults)."""
    return _company_row(db)


@router.put("/company-info", response_model=CompanyInfoRow)
def update_company_info(
    body: CompanyInfoBody,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin")),
):
    """Upsert the single company-info row; unspecified fields keep their
    current effective value. Audited; invalidates the render-context cache."""
    from app.models.platform.company_info import PlatformCompanyInfo

    current = company_info.get_company_info(db)
    row = db.get(PlatformCompanyInfo, 1)
    if row is None:
        row = PlatformCompanyInfo(
            id=1,
            legal_name=current.legal_name,
            operating_name=current.operating_name,
            brand_name=current.brand_name,
            lending_type=current.lending_type,
            contacts=current.contacts,
        )
        db.add(row)

    changed: dict = {}
    for field_name in (
        "legal_name", "operating_name", "brand_name", "lending_type",
        "logo_ref", "favicon_ref",
    ):
        value = getattr(body, field_name)
        if value is not None:
            changed[field_name] = value
            setattr(row, field_name, value)
    if body.contacts is not None:
        row.contacts = [c.model_dump() for c in body.contacts]
        changed["contacts"] = f"{len(row.contacts)} contact(s)"
    row.updated_by = _actor_id(user)
    db.flush()
    _audit(
        db,
        "admin_company_info_updated",
        _actor_id(user),
        {"changed_fields": sorted(changed.keys())},
    )
    db.commit()
    company_info.invalidate_cache()
    return _company_row(db)


# ---------------------------------------------------------------------------
# Business calendar
# ---------------------------------------------------------------------------


class CalendarOverrideBody(BaseModel):
    date: date
    province: str = Field(default="ALL", max_length=3)
    kind: str = Field(pattern="^(closure|business_day)$")
    label: Optional[str] = Field(default=None, max_length=200)


@router.get("/calendar/next-business-day")
def probe_next_business_day(
    on_or_after: date,
    province: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Scheduler probe: is the date a business day, and what's the next one?
    (Declared before /calendar/{year} so the literal path wins routing.)"""
    is_bd = business_calendar.is_business_day(on_or_after, province, db)
    nxt = business_calendar.next_business_day(on_or_after, province, db)
    return {
        "date": on_or_after.isoformat(),
        "province": (province or business_calendar.NATIONWIDE).upper(),
        "is_business_day": is_bd,
        "next_business_day": nxt.isoformat(),
    }


@router.get("/calendar/{year}")
def get_calendar(
    year: int,
    province: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Computed statutory holidays + admin overrides for a year (± province)."""
    if year < 2000 or year > 2100:
        raise HTTPException(status_code=422, detail="year out of range")
    return business_calendar.list_calendar(year, province, db)


@router.post("/calendar/overrides", status_code=201)
def add_calendar_override(
    body: CalendarOverrideBody,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin")),
):
    """Add a closure (or force-business-day) override. One override per
    date+province (409 on duplicates)."""
    from app.models.platform.business_calendar import PlatformBusinessCalendarOverride

    province = (body.province or "ALL").strip().upper()
    existing = (
        db.query(PlatformBusinessCalendarOverride)
        .filter(
            PlatformBusinessCalendarOverride.date == body.date,
            PlatformBusinessCalendarOverride.province == province,
        )
        .first()
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"an override already exists for {body.date} / {province}",
        )
    row = PlatformBusinessCalendarOverride(
        date=body.date,
        province=province,
        kind=body.kind,
        label=body.label,
        created_by=_actor_id(user),
    )
    db.add(row)
    db.flush()
    _audit(
        db,
        "admin_calendar_override_added",
        _actor_id(user),
        {"date": body.date.isoformat(), "province": province,
         "kind": body.kind, "label": body.label},
    )
    db.commit()
    return {
        "id": str(row.id), "date": row.date.isoformat(),
        "province": row.province, "kind": row.kind, "label": row.label,
    }


@router.delete("/calendar/overrides/{override_id}", status_code=200)
def remove_calendar_override(
    override_id: UUID,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin")),
):
    from app.models.platform.business_calendar import PlatformBusinessCalendarOverride

    row = db.get(PlatformBusinessCalendarOverride, override_id)
    if row is None:
        raise HTTPException(status_code=404, detail="override not found")
    payload = {"date": row.date.isoformat(), "province": row.province, "kind": row.kind}
    db.delete(row)
    _audit(db, "admin_calendar_override_removed", _actor_id(user), payload)
    db.commit()
    return {"deleted": str(override_id)}


# ---------------------------------------------------------------------------
# Notification matrix
# ---------------------------------------------------------------------------


class MatrixEventRow(BaseModel):
    notification_type: str
    audiences: list[str]
    cells: dict  # {audience: {channel: {"enabled": bool, "locked": bool}}}
    attachments: list[str]
    enabled: bool


class MatrixUpdate(BaseModel):
    audience_channels: Optional[dict] = Field(
        default=None,
        description='{"borrower": {"email": true, "sms": false}, ...} — '
        "dashboard cells cannot be turned off",
    )
    attachments: Optional[list[str]] = None


def _matrix_row(db: Session, ntype: str) -> MatrixEventRow:
    from app.services.notification_config import get_rule

    em = matrix_svc.get_event_matrix(db, ntype)
    cells: dict = {}
    for cell in em.cells:
        cells.setdefault(cell.audience, {})[cell.channel] = {
            "enabled": cell.enabled,
            "locked": cell.locked,
        }
    return MatrixEventRow(
        notification_type=ntype,
        audiences=list(em.audiences),
        cells=cells,
        attachments=list(em.attachments),
        enabled=get_rule(db, ntype).enabled,
    )


@router.get("/notification-matrix", response_model=list[MatrixEventRow])
def list_notification_matrix(db: Session = Depends(get_db)):
    """The full event catalog as a 4-audience × 3-channel grid."""
    from app.services.notification_render import NOTIFICATION_TYPES

    return [_matrix_row(db, ntype) for ntype in sorted(NOTIFICATION_TYPES)]


@router.put("/notification-matrix/{notification_type}", response_model=MatrixEventRow)
def update_notification_matrix(
    notification_type: str,
    body: MatrixUpdate,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin")),
):
    """Update matrix cells and/or the attachments list for one event. Cells are
    merged per audience; dashboard cells are rejected when turned off."""
    from app.models.platform.notification_rule import PlatformNotificationRule
    from app.services.notification_config import get_rule
    from app.services.notification_render import NOTIFICATION_TYPES

    if notification_type not in NOTIFICATION_TYPES:
        raise HTTPException(status_code=404, detail="unknown notification type")
    error = matrix_svc.validate_matrix_update(body.audience_channels, body.attachments)
    if error:
        raise HTTPException(status_code=422, detail=error)

    row = db.get(PlatformNotificationRule, notification_type)
    if row is None:
        current = get_rule(db, notification_type)
        row = PlatformNotificationRule(
            notification_type=notification_type,
            offsets=list(current.offsets),
            email_enabled="email" in current.enabled_channels,
            dashboard_enabled="dashboard" in current.enabled_channels,
            sms_enabled="sms" in current.enabled_channels,
            content_overrides=dict(current.content_overrides),
            enabled=current.enabled,
            audience_channels={},
            attachments=[],
        )
        db.add(row)

    if body.audience_channels is not None:
        merged = dict(row.audience_channels or {})
        for audience, chans in body.audience_channels.items():
            audience_cells = dict(merged.get(audience) or {})
            audience_cells.update(chans)
            merged[audience] = audience_cells
        row.audience_channels = merged
    if body.attachments is not None:
        row.attachments = list(dict.fromkeys(body.attachments))
    db.flush()
    _audit(
        db,
        "admin_notification_matrix_updated",
        _actor_id(user),
        {
            "notification_type": notification_type,
            "audience_channels": body.audience_channels,
            "attachments": body.attachments,
        },
    )
    db.commit()
    return _matrix_row(db, notification_type)
