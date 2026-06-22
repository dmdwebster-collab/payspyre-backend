"""Tests for the system-configurable notification config service.

Covers the DB-backed notification-rule resolution (with DunningPolicy fallback),
per-province send windows (the compliance MECHANISM), and borrower-province
lookup from platform_patient_fields. Postgres-backed; DO NOT run via this agent
(shared test DB).
"""
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from app.models.platform.notification_rule import PlatformNotificationRule
from app.models.platform.patient import PlatformPatient
from app.models.platform.patient_field import PlatformPatientField
from app.services.dunning import DEFAULT_POLICY
from app.services import notification_config as nc


# --- helpers ---------------------------------------------------------------


def _rule(db: Session, ntype: str, **kw) -> PlatformNotificationRule:
    defaults = dict(
        offsets=[7, 2],
        email_enabled=True,
        dashboard_enabled=False,
        sms_enabled=False,
        content_overrides={},
        enabled=True,
    )
    defaults.update(kw)
    row = PlatformNotificationRule(notification_type=ntype, **defaults)
    db.add(row)
    db.commit()
    return row


def _patient_with_province(db: Session, province: str) -> PlatformPatient:
    p = PlatformPatient(email="bw@example.com", legal_first_name="Bo", legal_last_name="W")
    db.add(p)
    db.commit()
    db.refresh(p)
    db.add(
        PlatformPatientField(
            patient_id=p.id, field_key="province", field_value=province,
            source="clinic", is_current=True,
        )
    )
    db.commit()
    return p


# --- rule resolution (DB-backed + dataclass fallback) ----------------------


class TestRuleResolution:
    def test_fallback_when_no_row(self, db_session: Session):
        # No DB row → DunningPolicy defaults (current cadence).
        rule = nc.get_rule(db_session, "payment_due_reminder")
        assert rule.offsets == (7, 2)
        assert rule.enabled_channels == ("email",)
        overdue = nc.get_rule(db_session, "payment_overdue")
        assert overdue.offsets == (7, 14, 21, 30, 60, 90)

    def test_db_row_overrides_offsets_and_channels(self, db_session: Session):
        _rule(db_session, "payment_due_reminder", offsets=[5, 1],
              email_enabled=True, dashboard_enabled=True, sms_enabled=False)
        rule = nc.get_rule(db_session, "payment_due_reminder")
        assert rule.offsets == (5, 1)
        # ordered email, dashboard, sms subset
        assert rule.enabled_channels == ("email", "dashboard")

    def test_disabled_rule_suppresses_all_channels(self, db_session: Session):
        _rule(db_session, "payment_overdue", enabled=False, email_enabled=True)
        assert nc.channel_enabled(db_session, "payment_overdue", "email") is False

    def test_per_channel_toggle(self, db_session: Session):
        _rule(db_session, "payment_overdue", email_enabled=False,
              dashboard_enabled=True, sms_enabled=True)
        assert nc.channel_enabled(db_session, "payment_overdue", "email") is False
        assert nc.channel_enabled(db_session, "payment_overdue", "dashboard") is True
        assert nc.channel_enabled(db_session, "payment_overdue", "sms") is True

    def test_content_override(self, db_session: Session):
        _rule(db_session, "payment_due_reminder",
              content_overrides={"dashboard": {"subject": "Heads up", "body": "Pay soon"}})
        ov = nc.content_override(db_session, "payment_due_reminder", "dashboard")
        assert ov == {"subject": "Heads up", "body": "Pay soon"}
        assert nc.content_override(db_session, "payment_due_reminder", "sms") is None


# --- per-province send windows (compliance MECHANISM, placeholder hours) ----


class TestSendWindow:
    def test_within_window_daytime(self):
        # 13:00 UTC == 09:00 EDT (Toronto, summer) → inside 08:00–21:00.
        now = datetime(2026, 7, 1, 13, 0, tzinfo=timezone.utc)
        assert nc.is_within_send_window("ON", now) is True

    def test_outside_window_night(self):
        # 03:00 UTC == 23:00 EDT previous day → outside the window.
        now = datetime(2026, 7, 1, 3, 0, tzinfo=timezone.utc)
        assert nc.is_within_send_window("ON", now) is False

    def test_unknown_province_is_permissive(self):
        now = datetime(2026, 7, 1, 3, 0, tzinfo=timezone.utc)
        assert nc.is_within_send_window(None, now) is True
        assert nc.is_within_send_window("XX", now) is True

    def test_all_canadian_codes_present(self):
        # Mechanism must cover every province/territory (placeholder hours).
        expected = {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU",
                    "ON", "PE", "QC", "SK", "YT"}
        assert set(nc.PROVINCE_SEND_WINDOWS) == expected

    def test_naive_datetime_treated_as_utc(self):
        # naive 13:00 assumed UTC → 09:00 EDT → inside.
        assert nc.is_within_send_window("ON", datetime(2026, 7, 1, 13, 0)) is True


# --- borrower province lookup (platform_patient_fields) --------------------


class TestProvinceLookup:
    def test_reads_current_province_field(self, db_session: Session):
        p = _patient_with_province(db_session, "BC")
        assert nc.get_patient_province(db_session, p.id) == "BC"

    def test_none_when_unset(self, db_session: Session):
        p = PlatformPatient(email="np@example.com")
        db_session.add(p)
        db_session.commit()
        db_session.refresh(p)
        assert nc.get_patient_province(db_session, p.id) is None

    def test_none_patient_id(self, db_session: Session):
        assert nc.get_patient_province(db_session, None) is None
