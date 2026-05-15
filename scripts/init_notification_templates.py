"""
Initialize default notification templates for PaySpyre.
Run this after database migration to populate notification templates.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.base import SessionLocal
from app.models.notification import NotificationTemplate

DEFAULT_TEMPLATES = [
    {
        "name": "application_approved_email",
        "type": "email",
        "category": "application_status",
        "subject": "Congratulations! Your Loan Application Has Been Approved",
        "body_template": None,
        "variables": [
            "borrower_name", "amount", "application_id", "interest_rate",
            "term", "monthly_payment", "agreement_url",
        ],
    },
    {
        "name": "application_rejected_email",
        "type": "email",
        "category": "application_status",
        "subject": "Update on Your Loan Application",
        "body_template": None,
        "variables": [
            "borrower_name", "amount", "application_id", "application_date",
            "rejection_reason", "specific_details",
        ],
    },
    {
        "name": "application_under_review_email",
        "type": "email",
        "category": "application_status",
        "subject": "Your Application is Under Review",
        "body_template": None,
        "variables": [
            "borrower_name", "amount", "application_id", "submitted_date",
            "estimated_decision_date", "kyc_completed_date",
        ],
    },
    {
        "name": "documents_required_email",
        "type": "email",
        "category": "application_status",
        "subject": "Action Required: Documents Needed for Your Application",
        "body_template": None,
        "variables": [
            "borrower_name", "amount", "application_id", "deadline",
            "days_remaining", "documents", "upload_url",
        ],
    },
    {
        "name": "payment_reminder_email",
        "type": "email",
        "category": "payment_reminder",
        "subject": "Payment Reminder: {{ days_until_due }} Days Until Due",
        "body_template": None,
        "variables": [
            "borrower_name", "loan_id", "payment_amount", "due_date",
            "days_until_due", "payment_url", "late_fee", "payment_method",
        ],
    },
    {
        "name": "payment_overdue_email",
        "type": "email",
        "category": "payment_reminder",
        "subject": "URGENT: Payment Overdue - Action Required",
        "body_template": None,
        "variables": [
            "borrower_name", "loan_id", "payment_amount", "due_date",
            "days_overdue", "late_fee", "payment_url",
        ],
    },
    {
        "name": "monthly_statement_email",
        "type": "email",
        "category": "statement",
        "subject": "Your Monthly Statement - {{ statement_period }}",
        "body_template": None,
        "variables": [
            "borrower_name", "statement_period", "loan_id", "account_number",
            "current_balance", "principal_balance", "interest_accrued",
            "next_payment", "next_due_date", "total_paid_ytd", "transactions",
            "payment_url", "account_url",
        ],
    },
    {
        "name": "urgent_action_required_email",
        "type": "email",
        "category": "urgent",
        "subject": "URGENT: {{ alert_title }} - Action Required",
        "body_template": None,
        "variables": [
            "borrower_name", "alert_title", "alert_message", "deadline",
            "time_remaining", "reference_id", "loan_application_id",
            "action_steps", "consequences", "action_url",
        ],
    },
    {
        "name": "application_approved_sms",
        "type": "sms",
        "category": "application_status",
        "body_template": "PaySpyre: Great news! Your loan application for {{ amount }} has been APPROVED. Sign your agreement at {{ agreement_url }} to receive funds. Reply HELP for help.",
        "variables": ["amount", "agreement_url"],
    },
    {
        "name": "application_rejected_sms",
        "type": "sms",
        "category": "application_status",
        "body_template": "PaySpyre: Your loan application for {{ amount }} was not approved. Contact us at 1-800-PAYSPYRE for details or to discuss options. We're here to help.",
        "variables": ["amount"],
    },
    {
        "name": "payment_reminder_sms",
        "type": "sms",
        "category": "payment_reminder",
        "body_template": "PaySpyre: Payment of {{ payment_amount }} due on {{ due_date }} ({{ days_until_due }} days). Pay now: {{ payment_url }} Avoid late fees. Reply STOP to opt out.",
        "variables": ["payment_amount", "due_date", "days_until_due", "payment_url"],
    },
    {
        "name": "payment_overdue_sms",
        "type": "sms",
        "category": "payment_reminder",
        "body_template": "URGENT PaySpyre: Payment OVERDUE by {{ days_overdue }} days. Amount: {{ payment_amount }}. Pay now: {{ payment_url }} Late fees apply. Call 1-800-PAYSPYRE.",
        "variables": ["days_overdue", "payment_amount", "payment_url"],
    },
    {
        "name": "urgent_action_required_sms",
        "type": "sms",
        "category": "urgent",
        "body_template": "URGENT PaySpyre: {{ alert_title }}. Action required by {{ deadline }}. {{ time_remaining }} remaining. Details: {{ action_url }} Call 1-800-PAYSPYRE immediately.",
        "variables": ["alert_title", "deadline", "time_remaining", "action_url"],
    },
    {
        "name": "documents_required_sms",
        "type": "sms",
        "category": "application_status",
        "body_template": "PaySpyre: We need documents to process your application. Upload by {{ deadline }}: {{ upload_url }} {{ document_count }} document(s) needed. Call 1-800-PAYSPYRE with questions.",
        "variables": ["deadline", "upload_url", "document_count"],
    },
]


def load_email_template(name: str) -> str:
    """Load email template from file."""
    import os

    template_dir = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "app",
        "templates",
        "emails",
    )

    template_file = os.path.join(template_dir, f"{name}.html")

    if os.path.exists(template_file):
        with open(template_file, "r", encoding="utf-8") as f:
            return f.read()

    return None


def init_templates():
    db = SessionLocal()

    try:
        created_count = 0
        updated_count = 0

        for template_data in DEFAULT_TEMPLATES:
            existing = db.query(NotificationTemplate).filter(
                NotificationTemplate.name == template_data["name"]
            ).first()

            if existing:
                for key, value in template_data.items():
                    if key != "body_template" or value is not None:
                        setattr(existing, key, value)
                updated_count += 1
            else:
                # Load email template from file if it's an email type
                if template_data["type"] == "email" and template_data["body_template"] is None:
                    template_file_name = template_data["name"].replace("_email", "")
                    template_data["body_template"] = load_email_template(template_file_name)

                if template_data["body_template"]:
                    template = NotificationTemplate(**template_data)
                    db.add(template)
                    created_count += 1
                else:
                    print(f"Warning: Could not load template for {template_data['name']}")

        db.commit()

        print(f"Successfully initialized notification templates:")
        print(f"  - Created: {created_count}")
        print(f"  - Updated: {updated_count}")
        print(f"  - Total: {created_count + updated_count}")

    except Exception as e:
        db.rollback()
        print(f"Error initializing templates: {e}")
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    init_templates()