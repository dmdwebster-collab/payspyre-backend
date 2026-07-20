"""Admin CRUD for system-document templates (WS-B — Turnkey "Settings >
Application process > System documents").

VERSIONED, NEVER DESTRUCTIVE: a template version is an immutable row. "Editing"
= POSTing a new version (new row, version+1 within the same kind+scope key);
old versions are kept forever as history. The only mutations are the
``active`` flag (deactivate pulls a version out of resolution; activate
restores it — Turnkey's history/restore affordance). There is NO delete and NO
body update endpoint, deliberately.

Resolution preview: ``POST /{id}/preview`` renders a template against a real
loan (or blank sample data) and reports unresolved merge fields — the safety
check before activating Dave's migrated TL templates.

Merge-field browser: ``GET /merge-fields`` serves the canonical dictionary
(scalar + table fields) that ``app/services/document_engine.py`` maintains.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status as http_status
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.auth import require_roles
from app.db.base import get_db
from app.models.platform.document_template import (
    DOCUMENT_KINDS,
    DOCUMENT_SCOPES,
    PlatformDocumentTemplate,
)
from app.models.platform.loan import PlatformLoan
from app.services import document_engine

router = APIRouter(dependencies=[Depends(require_roles("admin"))])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class TemplateSummary(BaseModel):
    id: UUID
    kind: str
    scope: str
    product_id: Optional[UUID] = None
    vendor_id: Optional[UUID] = None
    version: int
    title: str
    description: Optional[str] = None
    active: bool
    created_at: datetime


class TemplateDetail(TemplateSummary):
    body_html: str


class TemplateCreate(BaseModel):
    kind: str
    scope: str = "global"
    product_id: Optional[UUID] = None
    vendor_id: Optional[UUID] = None
    title: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = Field(None, max_length=1000)
    body_html: str = Field(..., min_length=1)


class TemplateList(BaseModel):
    templates: list[TemplateSummary]


class PreviewRequest(BaseModel):
    # Render against a real loan's data when provided; otherwise a blank
    # sample context (company fields resolve, loan fields render empty).
    loan_id: Optional[UUID] = None


class PreviewResponse(BaseModel):
    html: str
    unknown_fields: list[str]


class MergeFieldsResponse(BaseModel):
    scalar_fields: dict[str, dict[str, str]]
    table_fields: dict[str, dict]
    syntax: dict[str, str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _summary(t: PlatformDocumentTemplate) -> TemplateSummary:
    return TemplateSummary(
        id=t.id,
        kind=t.kind,
        scope=t.scope,
        product_id=t.product_id,
        vendor_id=t.vendor_id,
        version=t.version,
        title=t.title,
        description=t.description,
        active=t.active,
        created_at=t.created_at,
    )


def _get_template(db: Session, template_id: UUID) -> PlatformDocumentTemplate:
    t = (
        db.query(PlatformDocumentTemplate)
        .filter(PlatformDocumentTemplate.id == template_id)
        .first()
    )
    if t is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="Template not found"
        )
    return t


def _validate_scope(body: TemplateCreate) -> None:
    if body.kind not in DOCUMENT_KINDS:
        raise HTTPException(
            status_code=422, detail=f"kind must be one of {list(DOCUMENT_KINDS)}"
        )
    if body.scope not in DOCUMENT_SCOPES:
        raise HTTPException(
            status_code=422, detail=f"scope must be one of {list(DOCUMENT_SCOPES)}"
        )
    if body.scope == "global" and (body.product_id or body.vendor_id):
        raise HTTPException(
            status_code=422, detail="global scope must not set product_id or vendor_id"
        )
    if body.scope == "product" and (body.product_id is None or body.vendor_id):
        raise HTTPException(
            status_code=422, detail="product scope requires product_id (and no vendor_id)"
        )
    if body.scope == "vendor" and (body.vendor_id is None or body.product_id):
        raise HTTPException(
            status_code=422, detail="vendor scope requires vendor_id (and no product_id)"
        )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/merge-fields", response_model=MergeFieldsResponse)
def merge_fields() -> MergeFieldsResponse:
    """The canonical merge-field dictionary (Turnkey's Merge fields dialog)."""
    return MergeFieldsResponse(
        scalar_fields=document_engine.MERGE_FIELDS,
        table_fields=document_engine.TABLE_FIELDS,
        syntax={
            "scalar": "{{FieldName}} — substituted with the HTML-escaped value",
            "table": "{{Table:TableName}} — substituted with a rendered HTML table",
            "unknown": "Unknown placeholders render empty and are reported by preview",
        },
    )


@router.get("", response_model=TemplateList)
def list_templates(
    kind: Optional[str] = None,
    scope: Optional[str] = None,
    include_inactive: bool = True,
    db: Session = Depends(get_db),
) -> TemplateList:
    """Every template version (history included by default), newest first."""
    q = db.query(PlatformDocumentTemplate)
    if kind is not None:
        q = q.filter(PlatformDocumentTemplate.kind == kind)
    if scope is not None:
        q = q.filter(PlatformDocumentTemplate.scope == scope)
    if not include_inactive:
        q = q.filter(PlatformDocumentTemplate.active.is_(True))
    rows = q.order_by(
        PlatformDocumentTemplate.kind.asc(),
        PlatformDocumentTemplate.scope.asc(),
        PlatformDocumentTemplate.version.desc(),
        PlatformDocumentTemplate.created_at.desc(),
    ).all()
    return TemplateList(templates=[_summary(t) for t in rows])


@router.get("/{template_id}", response_model=TemplateDetail)
def get_template(template_id: UUID, db: Session = Depends(get_db)) -> TemplateDetail:
    t = _get_template(db, template_id)
    return TemplateDetail(**_summary(t).model_dump(), body_html=t.body_html)


@router.post("", response_model=TemplateDetail, status_code=http_status.HTTP_201_CREATED)
def create_template_version(
    body: TemplateCreate,
    db: Session = Depends(get_db),
    user=Depends(require_roles("admin")),
) -> TemplateDetail:
    """Create a NEW template version (append-only).

    Version = 1 + the highest existing version for the same (kind, scope,
    product/vendor key) — regardless of active flags, so history numbering
    never repeats. Existing rows are never modified.
    """
    _validate_scope(body)

    version_q = db.query(func.max(PlatformDocumentTemplate.version)).filter(
        PlatformDocumentTemplate.kind == body.kind,
        PlatformDocumentTemplate.scope == body.scope,
    )
    if body.scope == "product":
        version_q = version_q.filter(
            PlatformDocumentTemplate.product_id == body.product_id
        )
    elif body.scope == "vendor":
        version_q = version_q.filter(
            PlatformDocumentTemplate.vendor_id == body.vendor_id
        )
    next_version = (version_q.scalar() or 0) + 1

    t = PlatformDocumentTemplate(
        kind=body.kind,
        scope=body.scope,
        product_id=body.product_id,
        vendor_id=body.vendor_id,
        version=next_version,
        title=body.title,
        description=body.description,
        body_html=body.body_html,
        active=True,
        created_by=getattr(user, "id", None) if isinstance(getattr(user, "id", None), UUID) else None,
    )
    db.add(t)
    try:
        db.commit()
    except IntegrityError:
        # Concurrent version race on the partial unique index — ask to retry.
        db.rollback()
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="Concurrent template version created — retry",
        )
    db.refresh(t)
    return TemplateDetail(**_summary(t).model_dump(), body_html=t.body_html)


@router.post("/{template_id}/deactivate", response_model=TemplateSummary)
def deactivate_template(template_id: UUID, db: Session = Depends(get_db)) -> TemplateSummary:
    """Pull a version out of resolution (row is kept — restore any time)."""
    t = _get_template(db, template_id)
    t.active = False
    db.commit()
    db.refresh(t)
    return _summary(t)


@router.post("/{template_id}/activate", response_model=TemplateSummary)
def activate_template(template_id: UUID, db: Session = Depends(get_db)) -> TemplateSummary:
    """Restore a previously deactivated version into resolution."""
    t = _get_template(db, template_id)
    t.active = True
    db.commit()
    db.refresh(t)
    return _summary(t)


@router.post("/{template_id}/preview", response_model=PreviewResponse)
def preview_template(
    template_id: UUID,
    body: PreviewRequest,
    db: Session = Depends(get_db),
) -> PreviewResponse:
    """Render a template (any version) without persisting anything.

    With ``loan_id``: real merge data from that loan's graph. Without: blank
    sample data (company fields resolve; loan fields empty). Reports every
    placeholder that did not resolve so typos are caught before activation.
    """
    t = _get_template(db, template_id)
    if body.loan_id is not None:
        loan = db.query(PlatformLoan).filter(PlatformLoan.id == body.loan_id).first()
        if loan is None:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND, detail="Loan not found"
            )
        application, patient, product, vendor = document_engine._load_merge_entities(
            db, loan
        )
        context = document_engine.build_scalar_context(
            loan=loan, patient=patient, product=product, vendor=vendor
        )
        tables = document_engine.build_tables(loan=loan, product=product)
    else:
        context = document_engine.build_scalar_context()
        tables = document_engine.build_tables()
    result = document_engine.render_template(t.body_html, context, tables)
    return PreviewResponse(html=result.html, unknown_fields=list(result.unknown_fields))
