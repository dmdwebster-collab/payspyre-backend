"""Adverse-action (ECOA / FCRA) notice service — audit §7 (launch blocker).

When a credit application is **declined**, a regulated lender must deliver an
*adverse-action notice* to the applicant. This module builds that notice and
sends it. It is deliberately a **standalone, side-effect-only** module (mirrors
``loan_lifecycle``): it owns NO decisioning logic, only the post-decline
notification + audit.

What the notice must contain (legal requirements):

1. **Principal reasons for the denial** — the specific, accurate reasons credit
   was denied (ECOA / Reg B §1002.9; FCRA §615(a) where a bureau was used).
   We translate the engine's stable ``decision_reasons`` codes into plain
   applicant-facing sentences.
2. **Credit-bureau disclosure** — when a consumer report was used in the
   decision, FCRA §615(a) requires naming the consumer reporting agency (CRA)
   that furnished it, with its address + toll-free number, and a statement that
   the CRA did not make the decision and cannot explain it. When a real bureau
   was used we name it from settings; otherwise we emit a generic CRA
   disclosure.
3. **ECOA rights / non-discrimination statement** — the standard ECOA boilerplate
   (the federal agency notice that creditors may not discriminate on the basis
   of race, color, religion, national origin, sex, marital status, age, etc.),
   plus the FCRA consumer rights (free report within 60 days, right to dispute).

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

LEGAL-REVIEW CAVEAT: the ECOA/FCRA wording below is the standard federal
boilerplate (US Reg B / FCRA). PaySpyre operates in Canada (Equifax Canada /
TransUnion Canada), where the analogous regime is provincial consumer-reporting
legislation + PIPEDA rather than ECOA/FCRA. The task specified ECOA/FCRA wording
explicitly, so that is what is included here verbatim. **Counsel must confirm the
correct jurisdiction's adverse-action wording before this goes live** — the
content is structured so the boilerplate strings are the only thing that needs
swapping.
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
# plain-language "principal reason for denial" the applicant reads. ECOA / Reg B
# requires the *specific* reason, not a generic "you did not score high enough".
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
# ECOA / FCRA boilerplate (standard federal wording)
# ---------------------------------------------------------------------------

# ECOA non-discrimination notice — Reg B §1002.9(b)(1) prescribed wording.
ECOA_NOTICE = (
    "Notice: The federal Equal Credit Opportunity Act prohibits creditors from "
    "discriminating against credit applicants on the basis of race, color, "
    "religion, national origin, sex, marital status, age (provided the applicant "
    "has the capacity to enter into a binding contract); because all or part of "
    "the applicant's income derives from any public assistance program; or "
    "because the applicant has in good faith exercised any right under the "
    "Consumer Credit Protection Act. The federal agency that administers "
    "compliance with this law concerning this creditor is the Consumer Financial "
    "Protection Bureau, 1700 G Street NW, Washington, DC 20006."
)

# FCRA §615(a) consumer-report rights — free report within 60 days + right to
# dispute. Surfaced whenever a consumer report (bureau) contributed to the
# decision; the generic fallback keeps the structural disclosure intact.
FCRA_RIGHTS = (
    "In evaluating your application, a consumer report (credit report) may have "
    "been used. The consumer reporting agency named below did not make the "
    "decision to take the adverse action and is unable to provide you with the "
    "specific reasons why the action was taken. You have a right under the Fair "
    "Credit Reporting Act to know the information contained in your credit file "
    "at the consumer reporting agency. You have a right to a free copy of your "
    "report from the agency if you request it no later than 60 days after you "
    "receive this notice. You also have a right to dispute, directly with the "
    "consumer reporting agency, the accuracy or completeness of any information "
    "in your report."
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
        "ecoa_notice": ECOA_NOTICE,
        "fcra_rights": FCRA_RIGHTS,
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
        body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px; }}
        .header {{ background: linear-gradient(135deg, #666 0%, #444 100%); color: white; padding: 30px; text-align: center; border-radius: 10px 10px 0 0; }}
        .header h1 {{ margin: 0; font-size: 24px; }}
        .content {{ background: #f9f9f9; padding: 30px; border-radius: 0 0 10px 10px; }}
        .info-box {{ background: white; border-left: 4px solid #444; padding: 15px; margin: 20px 0; }}
        .reason-box {{ background: #fff; border: 1px solid #ddd; padding: 15px; border-radius: 5px; margin: 20px 0; }}
        .legal {{ font-size: 12px; color: #555; border-top: 1px solid #ddd; margin-top: 25px; padding-top: 15px; }}
        .footer {{ text-align: center; margin-top: 30px; color: #666; font-size: 12px; }}
    </style>
</head>
<body>
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
            <p><strong>Your rights under the Fair Credit Reporting Act</strong></p>
            <p>{content['fcra_rights']}</p>
            <p><strong>Equal Credit Opportunity Act notice</strong></p>
            <p>{content['ecoa_notice']}</p>
        </div>

        <div class="footer">
            <p>PaySpyre Financial | 123 Finance Street, Suite 100 | Kelowna, BC V1Y 1X1</p>
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
        "Your rights under the Fair Credit Reporting Act:",
        content["fcra_rights"],
        "",
        "Equal Credit Opportunity Act notice:",
        content["ecoa_notice"],
        "",
        "PaySpyre Financial | 123 Finance Street, Suite 100 | Kelowna, BC V1Y 1X1",
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


# ---------------------------------------------------------------------------
# Public entry point — the hook target
# ---------------------------------------------------------------------------


class _EmailServiceDispatcher:
    """Default dispatcher: a sync ``send_email`` over the async ``email_service``
    (Resend/SendGrid HTTP). Returns False / no-ops cleanly when email isn't
    configured. Used when the caller (the _decide hook) injects no dispatcher."""

    def send_email(self, to, subject, html_content, text_content=None):
        import asyncio

        from app.services.email_service import email_service

        return asyncio.run(
            email_service.send_email(
                to=to, subject=subject, html_content=html_content, text_content=text_content
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

        # Send via the injected dispatcher. Any vendor error is swallowed below.
        dispatcher.send_email(
            to=email,
            subject=EMAIL_SUBJECT,
            html_content=html,
            text_content=text_body,
        )

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
