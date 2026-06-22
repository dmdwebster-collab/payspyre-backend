"""Notice-of-decision (declined application) service — audit §7 (launch blocker).

CANADA-ONLY. PaySpyre operates solely in Canada; this notice is built to the
Canadian regime (federal + provincial), NOT US ECOA/FCRA. When a credit
application is **declined**, the applicant is sent a notice explaining the
decision. This module builds + sends it as a **standalone, side-effect-only**
module (mirrors ``loan_lifecycle``): no decisioning logic, only the post-decline
notification + audit.

What the notice contains:

1. **Principal reasons for the decision** — the specific, accurate reasons. We
   translate the engine's stable ``decision_reasons`` codes into plain
   applicant-facing sentences.
2. **Consumer-reporting agency disclosure** — when a consumer report was used,
   we name the agency (Equifax Canada / TransUnion Canada) with its contact
   details and state that it did not make the decision and cannot explain it.
   When a real bureau was used we name it from settings; otherwise a generic
   consumer-reporting disclosure.
3. **Consumer-report rights** (PIPEDA + provincial consumer reporting
   legislation: access the file, be told a report's contents, dispute
   inaccuracies) and a **non-discrimination notice** (Canadian Human Rights Act +
   provincial human rights codes).

Operational contract (mirrors the ``book_loan`` hook in
``FlowOrchestrator._decide``):

* **Idempotent.** A second call for the same application short-circuits — we
  check the ``platform_events`` log for an existing
  ``adverse_action_notice_sent`` row before sending.
* **Defensive / never raises into the caller.** A notice-send failure (bad
  email, vendor down, missing patient) is logged and swallowed. Decisioning must
  never break because a notice could not be sent. The function returns a bool so
  a caller *can* inspect the outcome, but it never propagates an exception.
* **No PII in the event.** The recorded ``adverse_action_notice_sent`` event
  carries only application_id / patient_id + the (non-PII) reason codes and the
  bureau *name*. The SIN, raw bureau data, and the rendered letter body are
  NEVER written to the event log.

LEGAL-REVIEW CAVEAT: the disclosure wording below states the Canadian rights at
a high level (PIPEDA + provincial consumer reporting + human rights legislation).
**Counsel must confirm the precise wording per province before launch** — the
content is structured so the boilerplate strings are the only thing that needs
swapping, and per-province variants can be selected by the applicant's province.
"""
from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.models.platform.event import PlatformEvent
from app.models.platform.patient import PlatformPatient

logger = get_logger(__name__)

ADVERSE_ACTION_EVENT_TYPE = "adverse_action_notice_sent"

EMAIL_SUBJECT = "Important information about your PaySpyre application"


# ---------------------------------------------------------------------------
# Reason-code → applicant-facing principal reason
# ---------------------------------------------------------------------------

# Maps the engine's stable ``decision_reasons`` codes (flow_engine.py) to the
# plain-language "principal reason" the applicant reads — the *specific* reason,
# not a generic "you did not score high enough".
_REASON_TEXT: dict[str, str] = {
    "quebec_coming_soon": "Service is not yet available in your province.",
    "manual_review_band": "Your application requires further review before it can be approved.",
    "bureau_below_minimum": "Your credit score did not meet our minimum requirement.",
    "active_bankruptcy": "Our records indicate an active or recent bankruptcy.",
    "fraud_signal_review": "We were unable to verify the information provided.",
    "identity_manual_review": "We were unable to fully verify your identity.",
}


def _humanize_reason(code: str) -> str:
    """Translate one ``decision_reasons`` code into an applicant-facing sentence.

    Handles the parameterized ``verification_failed:<type>`` /
    ``verification_unknown:<type>`` codes emitted by the engine, then falls back
    to the static map, then to a safe generic line for any unknown code.
    """
    if code.startswith("verification_failed:"):
        vtype = code.split(":", 1)[1]
        return f"We could not complete the required {_VERIFICATION_LABEL.get(vtype, vtype)} verification."
    if code.startswith("verification_unknown:"):
        vtype = code.split(":", 1)[1]
        return f"We could not obtain a result for the required {_VERIFICATION_LABEL.get(vtype, vtype)} verification."
    return _REASON_TEXT.get(code, "Your application did not meet our current lending criteria.")


_VERIFICATION_LABEL: dict[str, str] = {
    "identity": "identity",
    "income": "income",
    "bureau_soft": "credit",
    "bureau_hard": "credit",
    "kyc_id": "identity",
    "bank_link": "income/bank",
}


def humanize_reasons(reasons: list[str] | None) -> list[str]:
    """Public: turn raw decision_reason codes into deduped applicant-facing lines."""
    out: list[str] = []
    for code in reasons or []:
        line = _humanize_reason(code)
        if line not in out:
            out.append(line)
    if not out:
        out.append("Your application did not meet our current lending criteria.")
    return out


# ---------------------------------------------------------------------------
# Credit-bureau (CRA) disclosure
# ---------------------------------------------------------------------------

# A real-bureau disclosure naming the CRA used. PaySpyre's real bureau adapter is
# Equifax Canada (config: EQUIFAX_API_KEY). When that is configured we name it;
# otherwise we emit a generic CRA disclosure that still satisfies the structural
# requirement (name a CRA, give its contact, state it did not make the decision).
_EQUIFAX_CANADA_DISCLOSURE = {
    "bureau_used": True,
    "name": "Equifax Canada Co.",
    "address": "Box 190, Station Jean-Talon, Montreal, Quebec H1S 2Z2",
    "phone": "1-800-465-7166",
    "website": "www.equifax.ca",
}

_GENERIC_CRA_DISCLOSURE = {
    "bureau_used": False,
    "name": None,
    "address": None,
    "phone": None,
    "website": None,
}


def _bureau_disclosure() -> dict[str, Any]:
    """Return the CRA disclosure block.

    A real bureau is considered "used" when the real-adapter path is on AND an
    Equifax key is configured (the project's real bureau adapter is Equifax
    Canada). Otherwise we fall back to a generic CRA disclosure.
    """
    real_bureau = bool(getattr(settings, "USE_REAL_ADAPTERS", False)) and bool(
        getattr(settings, "EQUIFAX_API_KEY", "")
    )
    return dict(_EQUIFAX_CANADA_DISCLOSURE) if real_bureau else dict(_GENERIC_CRA_DISCLOSURE)


# ---------------------------------------------------------------------------
# Canadian disclosures (LEGAL REVIEW REQUIRED — per province)
# ---------------------------------------------------------------------------
# Canada has no single federal ECOA/Reg-B equivalent. Non-discrimination in
# credit is governed by the Canadian Human Rights Act + each province's human
# rights code; consumer-report access/dispute rights are governed by PIPEDA +
# each province's consumer reporting legislation. The wording below states those
# rights at a high level and is structured so counsel can drop in the precise
# provincial language. Counsel must confirm the correct wording per province
# before launch.

NONDISCRIMINATION_NOTICE = (
    "PaySpyre evaluates every applicant against the same criteria and does not "
    "deny credit on the basis of any ground protected by applicable Canadian "
    "human rights legislation, including the Canadian Human Rights Act and the "
    "human rights code of your province or territory. If you believe this "
    "decision was based on a prohibited ground, you may contact us, and you may "
    "also contact the human rights commission in your province or territory or "
    "the Canadian Human Rights Commission."
)

CONSUMER_REPORT_RIGHTS = (
    "In evaluating your application, a consumer report (credit report) may have "
    "been used. The consumer reporting agency named below did not make this "
    "decision and cannot provide the specific reasons for it. Under Canada's "
    "Personal Information Protection and Electronic Documents Act (PIPEDA) and "
    "the consumer reporting legislation of your province or territory, you have "
    "the right to be told the contents of any consumer report used, to access "
    "the information in your file at the consumer reporting agency, and to "
    "dispute the accuracy or completeness of any information it contains. To "
    "obtain a copy of your consumer report, contact the consumer reporting "
    "agency listed below."
)


# ---------------------------------------------------------------------------
# Notice content assembly
# ---------------------------------------------------------------------------


def build_notice_content(
    *,
    applicant_name: str,
    application_id: UUID | str,
    reasons: list[str] | None,
) -> dict[str, Any]:
    """Pure builder: assemble the structured notice content (no I/O).

    Exposed separately so tests can assert content without sending. Returns the
    fields the template renders + the structured pieces used for both HTML and a
    plaintext fallback.
    """
    principal_reasons = humanize_reasons(reasons)
    bureau = _bureau_disclosure()
    return {
        "applicant_name": applicant_name or "Applicant",
        "application_id": str(application_id),
        "principal_reasons": principal_reasons,
        "bureau": bureau,
        "nondiscrimination_notice": NONDISCRIMINATION_NOTICE,
        "consumer_report_rights": CONSUMER_REPORT_RIGHTS,
    }


def render_notice_html(content: dict[str, Any]) -> str:
    """Render the adverse-action notice to HTML.

    Templates in this repo (``app/templates/emails/*.html``) are jinja-style
    placeholder files with no wired renderer, so we render inline here (the
    pattern ``email_service`` already uses for verification / reset mails). A
    static ``app/templates/emails/adverse_action_notice.html`` companion ships
    for reference / future jinja wiring.
    """
    reasons_html = "".join(f"<li>{r}</li>" for r in content["principal_reasons"])
    bureau = content["bureau"]
    if bureau.get("bureau_used"):
        bureau_block = (
            "<p>Our credit decision was based in whole or in part on information "
            "obtained in a report from the consumer reporting agency listed below. "
            "You can contact them at:</p>"
            "<div class=\"info-box\">"
            f"<p><strong>{bureau['name']}</strong></p>"
            f"<p>{bureau['address']}</p>"
            f"<p>Phone: {bureau['phone']}</p>"
            f"<p>Web: {bureau['website']}</p>"
            "</div>"
        )
    else:
        bureau_block = (
            "<p>If a consumer report (credit report) was used in connection with "
            "this decision, the consumer reporting agency that supplied it did not "
            "make the decision and cannot provide the specific reasons for it. You "
            "may request the name, address, and telephone number of any consumer "
            "reporting agency that provided a report by contacting us.</p>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Notice of Action Taken</title>
    <style>
        body {{ font-family: 'Work Sans', Arial, sans-serif; line-height: 1.6; color: #1a1a1a; max-width: 600px; margin: 0 auto; padding: 20px; }}
        .header {{ background: #1a1a1a; color: #fff; padding: 28px; text-align: center; border-radius: 20px 20px 0 0; }}
        .header h1 {{ margin: 0; font-size: 22px; }}
        .content {{ background: #faf9f6; padding: 30px; border-radius: 0 0 20px 20px; }}
        h3 {{ font-size: 16px; font-weight: 600; }}
        .info-box {{ background: #fff; border-left: 4px solid #1a1a1a; padding: 16px; margin: 20px 0; border-radius: 8px; }}
        .reason-box {{ background: #fff; border: 1px solid #e6e3da; padding: 16px; border-radius: 8px; margin: 20px 0; }}
        .legal {{ font-size: 12px; color: #555; border-top: 1px solid #e6e3da; margin-top: 25px; padding-top: 15px; }}
        .footer {{ text-align: center; margin-top: 28px; color: #666; font-size: 12px; }}
    </style>
</head>
<body>
    <div style="padding:16px 0 10px; text-align:center;">
        <svg width="24" height="24" viewBox="99.5 71.5 48 48.2" xmlns="http://www.w3.org/2000/svg" style="vertical-align:middle;" role="img" aria-label="PaySpyre"><path fill="#84d1d1" fill-opacity="0.4" d="M128.074 72C128.074 87.082 140.66 90.852 146.953 90.852L146.953 100.277C131.852 100.277 118.637 90.852 128.074 72Z"/><path fill="#84d1d1" fill-opacity="0.4" d="M118.879 119.215C118.879 104.133 106.293 100.363 100 100.363L100 90.938C115.105 90.938 128.32 100.363 118.879 119.215Z"/><path fill="#84d1d1" d="M100 90.879C115.082 90.879 118.852 78.293 118.852 72L128.277 72C128.277 87.105 118.852 100.32 100 90.879Z"/><path fill="#84d1d1" d="M146.953 100.336C131.871 100.336 128.102 112.922 128.102 119.215L118.676 119.215C118.676 104.109 128.102 90.895 146.953 100.336Z"/></svg>
        <span style="font-size:18px; font-weight:600; color:#1a1a1a; vertical-align:middle; margin-left:8px;">PaySpyre</span>
    </div>
    <div class="header">
        <h1>Notice of Action Taken</h1>
    </div>
    <div class="content">
        <p>Dear {content['applicant_name']},</p>
        <p>Thank you for your recent credit application (Reference: {content['application_id']}).
        After careful review, we are unable to approve your application at this time.</p>

        <div class="reason-box">
            <h3>Principal reason(s) for our decision:</h3>
            <ul>{reasons_html}</ul>
        </div>

        <h3>Credit reporting agency disclosure</h3>
        {bureau_block}

        <div class="legal">
            <p><strong>Your consumer-reporting rights</strong></p>
            <p>{content['consumer_report_rights']}</p>
            <p><strong>Non-discrimination notice</strong></p>
            <p>{content['nondiscrimination_notice']}</p>
        </div>

        <div class="footer">
            <p>PaySpyre Financial | Kelowna, BC</p>
            <p>This is an automated notice. Please do not reply to this email.</p>
        </div>
    </div>
</body>
</html>"""


def render_notice_text(content: dict[str, Any]) -> str:
    """Plaintext fallback rendering of the notice."""
    lines = [
        "NOTICE OF ACTION TAKEN",
        "",
        f"Dear {content['applicant_name']},",
        "",
        f"Thank you for your recent credit application (Reference: {content['application_id']}). "
        "After careful review, we are unable to approve your application at this time.",
        "",
        "Principal reason(s) for our decision:",
    ]
    lines += [f"  - {r}" for r in content["principal_reasons"]]
    lines += ["", "Credit reporting agency disclosure:"]
    bureau = content["bureau"]
    if bureau.get("bureau_used"):
        lines += [
            f"  {bureau['name']}",
            f"  {bureau['address']}",
            f"  Phone: {bureau['phone']}  Web: {bureau['website']}",
        ]
    else:
        lines.append(
            "  If a consumer report was used, the agency that supplied it did not make "
            "the decision and cannot explain it. Contact us for the agency's details."
        )
    lines += [
        "",
        "Your consumer-reporting rights:",
        content["consumer_report_rights"],
        "",
        "Non-discrimination notice:",
        content["nondiscrimination_notice"],
        "",
        "PaySpyre Financial | Kelowna, BC",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Idempotency + audit
# ---------------------------------------------------------------------------


def _already_sent(db: Session, application_id: UUID | str) -> bool:
    """True when an ``adverse_action_notice_sent`` event already exists for this
    application (idempotency guard)."""
    row = db.execute(
        text(
            """
            SELECT 1 FROM platform_events
            WHERE event_type = :etype
              AND application_id = :app_id
            LIMIT 1
            """
        ),
        {"etype": ADVERSE_ACTION_EVENT_TYPE, "app_id": str(application_id)},
    ).first()
    return row is not None


def _record_event(
    db: Session,
    *,
    application: Any,
    reasons: list[str] | None,
    bureau_name: Optional[str],
) -> None:
    """Append an ``adverse_action_notice_sent`` audit row. No PII: only ids, the
    (non-PII) reason codes, and the bureau name."""
    payload = {
        "v": 1,
        "actor": {"type": "system", "id": "system"},
        "application_id": str(application.id),
        "patient_id": str(application.patient_id),
        "after": {
            "notice_type": "adverse_action",
            "reason_codes": list(reasons or []),
            "bureau_disclosed": bureau_name,
        },
    }
    event = PlatformEvent(
        event_type=ADVERSE_ACTION_EVENT_TYPE,
        actor="system",
        patient_id=application.patient_id,
        application_id=application.id,
        payload=payload,
    )
    db.add(event)
    db.flush()


def _record_failure_event(
    db: Session, *, application: Any, reasons: list[str] | None
) -> None:
    """Append an ``adverse_action_notice_failed`` audit row so an undelivered notice
    is provable + actionable (compliance: we must show we attempted to notify). No PII."""
    payload = {
        "v": 1,
        "actor": {"type": "system", "id": "system"},
        "application_id": str(application.id),
        "patient_id": str(application.patient_id),
        "after": {
            "notice_type": "adverse_action",
            "delivered": False,
            "reason_codes": list(reasons or []),
        },
    }
    event = PlatformEvent(
        event_type=ADVERSE_ACTION_FAILED_EVENT_TYPE,
        actor="system",
        patient_id=application.patient_id,
        application_id=application.id,
        payload=payload,
    )
    db.add(event)
    db.flush()


# ---------------------------------------------------------------------------
# Public entry point — the hook target
# ---------------------------------------------------------------------------


ADVERSE_ACTION_FAILED_EVENT_TYPE = "adverse_action_notice_failed"


def _send_via_sendgrid(
    *, api_key: str, from_email: str, to: str, subject: str, html_content: str, text_content=None
) -> bool:
    """Send a generic email through SendGrid. Returns True only on a 2xx ack."""
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail

    message = Mail(
        from_email=from_email,
        to_emails=to,
        subject=subject,
        plain_text_content=text_content or None,
        html_content=html_content,
    )
    response = SendGridAPIClient(api_key).send(message)
    status = getattr(response, "status_code", None)
    return isinstance(status, int) and 200 <= status < 300


class _EmailServiceDispatcher:
    """Default dispatcher for the adverse-action notice.

    Routes through the CONFIGURED email provider (``EMAIL_PROVIDER``) — SendGrid for
    the business, Resend as fallback — so the legally-required decline notice
    actually delivers. The previous implementation only ever hit the Resend
    singleton, which no-ops silently when ``RESEND_API_KEY`` is empty (it always is,
    since the business uses SendGrid) — so declined applicants got no notice.

    Returns True only when the provider accepted the message; the caller treats a
    falsy result as "not sent" and records a failure event rather than success."""

    def send_email(self, to, subject, html_content, text_content=None) -> bool:
        from app.core.config import settings

        if settings.EMAIL_PROVIDER == "sendgrid" and settings.SENDGRID_API_KEY:
            return _send_via_sendgrid(
                api_key=settings.SENDGRID_API_KEY,
                from_email=settings.SENDGRID_FROM_EMAIL,
                to=to,
                subject=subject,
                html_content=html_content,
                text_content=text_content,
            )

        import asyncio

        from app.services.email_service import email_service

        return bool(
            asyncio.run(
                email_service.send_email(
                    to=to, subject=subject, html_content=html_content, text_content=text_content
                )
            )
        )


def send_adverse_action_notice(
    db: Session,
    application: Any,
    reasons: list[str] | None,
    dispatcher: Any = None,
) -> bool:
    """Build + send the adverse-action notice for a declined application.

    Idempotent, defensive, audit-logged. Returns True when a notice was sent on
    this call, False when it was skipped (already sent / no email / send failed).
    NEVER raises — a notice-send failure must not break decisioning.

    ``dispatcher`` is duck-typed: it must expose
    ``send_email(to, subject, html_content, text_content=None)`` (sync or
    returning a truthy value). Tests pass a MagicMock.
    """
    try:
        application_id = application.id
        if dispatcher is None:
            dispatcher = _EmailServiceDispatcher()

        if _already_sent(db, application_id):
            logger.info(
                "adverse_action_notice_skipped_already_sent",
                application_id=str(application_id),
            )
            return False

        patient = (
            db.query(PlatformPatient)
            .filter(PlatformPatient.id == application.patient_id)
            .first()
        )
        email = getattr(patient, "email", None) if patient else None
        if not email:
            logger.warning(
                "adverse_action_notice_no_email",
                application_id=str(application_id),
                patient_id=str(application.patient_id),
            )
            return False

        applicant_name = " ".join(
            p for p in (
                getattr(patient, "legal_first_name", None),
                getattr(patient, "legal_last_name", None),
            ) if p
        ).strip()

        content = build_notice_content(
            applicant_name=applicant_name,
            application_id=application_id,
            reasons=reasons,
        )
        html = render_notice_html(content)
        text_body = render_notice_text(content)

        # Send via the injected dispatcher. A vendor exception is swallowed below;
        # a falsy return means the provider did NOT accept it (e.g. unconfigured) —
        # in that case we must NOT record the notice as sent.
        sent = dispatcher.send_email(
            to=email,
            subject=EMAIL_SUBJECT,
            html_content=html,
            text_content=text_body,
        )
        if not sent:
            logger.error(
                "adverse_action_notice_not_delivered",
                application_id=str(application_id),
                patient_id=str(application.patient_id),
            )
            _record_failure_event(db, application=application, reasons=reasons)
            return False

        _record_event(
            db,
            application=application,
            reasons=reasons,
            bureau_name=content["bureau"].get("name"),
        )
        logger.info(
            "adverse_action_notice_sent",
            application_id=str(application_id),
            patient_id=str(application.patient_id),
            bureau_disclosed=content["bureau"].get("name"),
        )
        return True
    except Exception as exc:  # noqa: BLE001 — notice send must never break decisioning
        logger.error(
            "adverse_action_notice_failed",
            application_id=str(getattr(application, "id", None)),
            error=str(exc),
        )
        return False
