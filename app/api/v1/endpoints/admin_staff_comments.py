"""Internal staff comments — Dave's **Comments** tab (application + loan).

    *"This is the communications tab to record and discuss the loan account.
    These communications are internal only."*

STAFF-ONLY. Every route here is gated on ``require_roles("admin", "staff")``,
and this router is mounted ONLY under the admin API. There is no clinic route
and no borrower-portal route to this data — the vendor surface
(``app/api/clinic/v1``) never imports this module or its model, which
``tests/test_vendor_visibility_fence.py`` keeps structurally true for the
clinic schemas and ``tests/test_staff_comments.py`` asserts directly.

NOT the same surface as either of its neighbours:

* ``/admin/communications`` — messages actually SENT to a borrower.
* ``/admin/applications/{id}/messages`` — the vendor⇄PaySpyre chat, which a
  clinic staffer reads and writes.

A staff comment is sent to nobody and is visible to nobody outside PaySpyre.

APPEND-ONLY: create + list, no PATCH, no DELETE. An internal note about a
credit decision is a record of what an operator believed at a moment in time;
if it can be edited it stops being evidence, because a rewritten note is
indistinguishable from an original one. Corrections are posted as follow-up
comments. Every post is mirrored into ``platform_events`` so the note is also
visible from the file's audit timeline.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import require_roles
from app.db.base import get_db
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.event import PlatformEvent
from app.models.platform.loan import PlatformLoan
from app.models.platform.staff_comment import COMMENT_MAX_LEN, PlatformStaffComment
from app.models.user import User

router = APIRouter(tags=["admin-staff-comments"])


class StaffCommentCreate(BaseModel):
    body: str = Field(..., min_length=1, max_length=COMMENT_MAX_LEN)


class StaffCommentOut(BaseModel):
    id: UUID
    #: Subject of the comment. Exactly one of these is set on a stored row;
    #: both can appear in a LOAN listing when ``include_application=true``
    #: replays the originating application's thread.
    application_id: Optional[UUID] = None
    loan_id: Optional[UUID] = None
    #: ``"application"`` | ``"loan"`` — which tab the comment was written on.
    subject: str
    author_user_id: UUID
    author_name: str
    body: str
    created_at: datetime
    #: True when the current viewer wrote it (bubble alignment).
    mine: bool = False


def _author_name(user: Optional[User]) -> str:
    if user is None:
        return "PaySpyre"
    name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    return name or (user.email or "PaySpyre")


def _to_out(
    comment: PlatformStaffComment,
    author: Optional[User],
    viewer_user_id: UUID,
) -> StaffCommentOut:
    return StaffCommentOut(
        id=comment.id,
        application_id=comment.application_id,
        loan_id=comment.loan_id,
        subject="application" if comment.application_id else "loan",
        author_user_id=comment.author_user_id,
        author_name=_author_name(author),
        body=comment.body,
        created_at=comment.created_at,
        mine=comment.author_user_id == viewer_user_id,
    )


def _authors(db: Session, comments: list[PlatformStaffComment]) -> dict[UUID, User]:
    ids = {c.author_user_id for c in comments}
    if not ids:
        return {}
    return {u.id: u for u in db.query(User).filter(User.id.in_(ids)).all()}


def _serialize(
    db: Session, comments: list[PlatformStaffComment], viewer_user_id: UUID
) -> list[StaffCommentOut]:
    authors = _authors(db, comments)
    return [_to_out(c, authors.get(c.author_user_id), viewer_user_id) for c in comments]


def _application_or_404(db: Session, application_id: UUID) -> PlatformCreditApplication:
    row = (
        db.query(PlatformCreditApplication)
        .filter(PlatformCreditApplication.id == application_id)
        .first()
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Application not found"
        )
    return row


def _loan_or_404(db: Session, loan_id: UUID) -> PlatformLoan:
    row = db.query(PlatformLoan).filter(PlatformLoan.id == loan_id).first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Loan not found"
        )
    return row


def _audit(
    db: Session,
    *,
    comment: PlatformStaffComment,
    actor: str,
    application_id: Optional[UUID],
    patient_id: Optional[UUID],
    subject: str,
) -> None:
    """Mirror the post into the file's audit timeline.

    The event payload carries the comment id and a short preview, never the
    full body: ``platform_events`` is a widely-read timeline and the note's
    canonical home is ``platform_staff_comments``.
    """
    db.add(
        PlatformEvent(
            event_type="staff_comment.posted",
            actor=actor,
            patient_id=patient_id,
            application_id=application_id,
            payload={
                "comment_id": str(comment.id),
                "subject": subject,
                "loan_id": str(comment.loan_id) if comment.loan_id else None,
                "body_preview": comment.body[:140],
                "body_length": len(comment.body),
            },
        )
    )


# ---------------------------------------------------------------------------
# Application thread
# ---------------------------------------------------------------------------


@router.get(
    "/applications/{application_id}/comments",
    response_model=list[StaffCommentOut],
    summary="Internal staff comments on an application (oldest first)",
)
def list_application_comments(
    application_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "staff")),
):
    _application_or_404(db, application_id)
    rows = (
        db.query(PlatformStaffComment)
        .filter(PlatformStaffComment.application_id == application_id)
        .order_by(PlatformStaffComment.created_at.asc(), PlatformStaffComment.id.asc())
        .all()
    )
    return _serialize(db, rows, user.id)


@router.post(
    "/applications/{application_id}/comments",
    response_model=StaffCommentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Post an internal staff comment on an application",
)
def post_application_comment(
    application_id: UUID,
    body: StaffCommentCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "staff")),
):
    application = _application_or_404(db, application_id)
    comment = PlatformStaffComment(
        application_id=application.id,
        author_user_id=user.id,
        body=body.body,
    )
    db.add(comment)
    db.flush()
    _audit(
        db,
        comment=comment,
        actor=user.email or str(user.id),
        application_id=application.id,
        patient_id=application.patient_id,
        subject="application",
    )
    db.commit()
    db.refresh(comment)
    return _to_out(comment, user, user.id)


# ---------------------------------------------------------------------------
# Loan thread
# ---------------------------------------------------------------------------


@router.get(
    "/loans/{loan_id}/comments",
    response_model=list[StaffCommentOut],
    summary="Internal staff comments on a loan (oldest first)",
)
def list_loan_comments(
    loan_id: UUID,
    db: Session = Depends(get_db),
    include_application: bool = Query(
        False,
        description=(
            "Also replay the originating application's comment thread, merged "
            "in chronological order. Read-only continuity for the servicing "
            "workspace; posts still land on the loan."
        ),
    ),
    user: User = Depends(require_roles("admin", "staff")),
):
    loan = _loan_or_404(db, loan_id)
    query = db.query(PlatformStaffComment)
    if include_application and loan.application_id is not None:
        query = query.filter(
            (PlatformStaffComment.loan_id == loan_id)
            | (PlatformStaffComment.application_id == loan.application_id)
        )
    else:
        query = query.filter(PlatformStaffComment.loan_id == loan_id)
    rows = query.order_by(
        PlatformStaffComment.created_at.asc(), PlatformStaffComment.id.asc()
    ).all()
    return _serialize(db, rows, user.id)


@router.post(
    "/loans/{loan_id}/comments",
    response_model=StaffCommentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Post an internal staff comment on a loan",
)
def post_loan_comment(
    loan_id: UUID,
    body: StaffCommentCreate,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles("admin", "staff")),
):
    loan = _loan_or_404(db, loan_id)
    comment = PlatformStaffComment(
        loan_id=loan.id,
        author_user_id=user.id,
        body=body.body,
    )
    db.add(comment)
    db.flush()
    _audit(
        db,
        comment=comment,
        actor=user.email or str(user.id),
        # The event is filed against the loan's application so it lands on the
        # same customer timeline the rest of the file uses.
        application_id=loan.application_id,
        patient_id=loan.patient_id,
        subject="loan",
    )
    db.commit()
    db.refresh(comment)
    return _to_out(comment, user, user.id)
