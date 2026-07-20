"""MSA / vendor-contract expiry alerts (WS-G — Dave, video 09 f0058: the
vendor Documents box "should auto-populate... I'd like expiry/renewal-date
tracking + notifications").

``scan_and_alert`` is the job body (``python -m app.jobs.vendor_doc_expiry``,
same external-cron model as every other job): it walks live vendor documents
that carry an ``expiry_date``, and for each 60/30/7-day threshold that has come
due it

  1. inserts a ``platform_vendor_document_expiry_alerts`` dedupe row (unique on
     (document, threshold) — the scan is idempotent and safe to re-run daily);
  2. appends a ``vendor_document_expiring`` ``platform_events`` audit row; and
  3. best-effort emails the clinic's staff users (falling back to the vendor
     contact email) plus the PaySpyre ops inbox — the exact recipient +
     inert-by-default model of ``message_notifications`` (nothing sends unless
     ``USE_REAL_NOTIFICATIONS`` is on or a fake ``sender`` is injected, so this
     is dormant until Dave's SendGrid creds land; the dedupe row + event are
     written regardless, so no alert is ever silently lost).

``due_thresholds`` is pure (DB-free tested). An already-expired document gets
one final ``0``-day ("expired") alert instead of the missed thresholds.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.models.loan import Vendor
from app.models.platform.crm import (
    PlatformVendorDocument,
    PlatformVendorDocumentExpiryAlert,
)
from app.models.platform.event import PlatformEvent

logger = get_logger(__name__)

# Days-before-expiry at which an alert fires. 0 = the "it has expired" notice.
EXPIRY_ALERT_THRESHOLDS: tuple[int, ...] = (60, 30, 7, 0)

EXPIRING_EVENT = "vendor_document_expiring"


def due_thresholds(
    expiry_date: date,
    today: date,
    already_sent: Iterable[int] = (),
) -> list[int]:
    """PURE: the alert threshold currently due for a document (0 or 1 entries).

    A threshold ``t`` is due when ``days_left <= t`` and no alert for ``t`` (or
    any smaller / more-urgent threshold) has been sent. Only the SINGLE
    most-urgent unsent threshold is returned — a document first seen 5 days out
    gets one 7-day alert, not a 60+30+7 burst; a document seen after expiry
    gets the one 0-day "expired" notice.
    """
    days_left = (expiry_date - today).days
    sent = set(already_sent)
    # A threshold t is eligible when we are inside its window (days_left <= t)
    # and nothing equally-or-more urgent (s <= t) has already been sent.
    eligible = [
        t
        for t in EXPIRY_ALERT_THRESHOLDS
        if days_left <= t and not any(s <= t for s in sent)
    ]
    return [min(eligible)] if eligible else []


def scan_and_alert(
    db: Session,
    *,
    today: Optional[date] = None,
    sender: Optional[object] = None,
) -> int:
    """One idempotent pass over live, expiring vendor documents.

    Returns the number of alerts recorded. Commits once at the end.
    """
    today = today or datetime.now(timezone.utc).date()

    rows = (
        db.query(PlatformVendorDocument, Vendor)
        .join(Vendor, PlatformVendorDocument.vendor_id == Vendor.id)
        .filter(
            PlatformVendorDocument.deleted_at.is_(None),
            PlatformVendorDocument.status == "uploaded",
            PlatformVendorDocument.expiry_date.isnot(None),
        )
        .all()
    )

    alerts_recorded = 0
    for doc, vendor in rows:
        sent = [
            t
            for (t,) in db.query(PlatformVendorDocumentExpiryAlert.threshold_days)
            .filter(PlatformVendorDocumentExpiryAlert.document_id == doc.id)
            .all()
        ]
        for threshold in due_thresholds(doc.expiry_date, today, sent):
            db.add(
                PlatformVendorDocumentExpiryAlert(
                    document_id=doc.id, threshold_days=threshold
                )
            )
            db.add(
                PlatformEvent(
                    event_type=EXPIRING_EVENT,
                    actor="system",
                    payload={
                        "v": 1,
                        "actor": {"type": "system", "id": "vendor_doc_expiry"},
                        "after": {
                            "vendor_id": str(doc.vendor_id),
                            "document_id": str(doc.id),
                            "doc_type": doc.doc_type,
                            "title": doc.title,
                            "expiry_date": doc.expiry_date.isoformat(),
                            "threshold_days": threshold,
                        },
                    },
                )
            )
            _notify(db, doc=doc, vendor=vendor, threshold=threshold, sender=sender)
            alerts_recorded += 1

    db.commit()
    logger.info("vendor_doc_expiry_scan_complete", alerts_recorded=alerts_recorded)
    return alerts_recorded


# ---------------------------------------------------------------------------
# Email (best-effort, inert by default — mirrors message_notifications)
# ---------------------------------------------------------------------------


def _notify(
    db: Session,
    *,
    doc: PlatformVendorDocument,
    vendor: Vendor,
    threshold: int,
    sender: Optional[object],
) -> None:
    """Best-effort email; swallows all errors (the dedupe row + event are the
    durable record — email is a nudge, not the system of record)."""
    try:
        if sender is None and not settings.USE_REAL_NOTIFICATIONS:
            return
        recipients = _resolve_recipients(db, vendor)
        if not recipients:
            logger.info(
                "vendor_doc_expiry_no_recipients", vendor_id=str(vendor.id)
            )
            return
        if sender is None:
            from app.services.message_notifications import _build_email_sender

            sender = _build_email_sender(db)
        subject, html = render_alert(
            vendor_name=vendor.business_name,
            title=doc.title,
            doc_type=doc.doc_type,
            expiry_date=doc.expiry_date,
            threshold=threshold,
        )
        for to_email in recipients:
            try:
                sender.send_message(to_email=to_email, subject=subject, html=html)
            except Exception as exc:  # one bad address must not skip the rest
                logger.warning(
                    "vendor_doc_expiry_email_failed",
                    document_id=str(doc.id),
                    error=str(exc.__class__.__name__),
                )
    except Exception as exc:  # pragma: no cover - defensive: never break the scan
        logger.warning(
            "vendor_doc_expiry_notify_error",
            document_id=str(doc.id),
            error=str(exc.__class__.__name__),
        )


def _resolve_recipients(db: Session, vendor: Vendor) -> list[str]:
    """Clinic staff emails (fallback: vendor contact email) + the ops inbox."""
    from app.services.clinic_membership import resolve_clinic_user_emails

    recipients = resolve_clinic_user_emails(db, vendor.id)
    if not recipients and vendor.email:
        recipients = [vendor.email]
    inbox = (getattr(settings, "PLATFORM_MESSAGES_INBOX", "") or "").strip()
    if inbox and inbox not in recipients:
        recipients = recipients + [inbox]
    return recipients


def render_alert(
    *,
    vendor_name: str,
    title: str,
    doc_type: str,
    expiry_date: date,
    threshold: int,
) -> tuple[str, str]:
    """PURE: (subject, html) for an expiry alert email."""
    label = doc_type.replace("_", " ").upper() if doc_type == "msa" else doc_type.replace("_", " ")
    when = expiry_date.isoformat()
    if threshold == 0:
        subject = f"PaySpyre: {vendor_name} — {label} document has EXPIRED"
        lead = f"expired on {when}"
    else:
        subject = (
            f"PaySpyre: {vendor_name} — {label} document expires in {threshold} days"
        )
        lead = f"expires on {when}"
    html = (
        f"<p>The document <strong>{title}</strong> ({label}) on file for "
        f"<strong>{vendor_name}</strong> {lead}.</p>"
        "<p>Please arrange renewal and upload the updated document in the "
        "PaySpyre console.</p>"
    )
    return subject, html
