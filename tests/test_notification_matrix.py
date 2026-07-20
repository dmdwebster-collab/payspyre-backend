"""WS-F notification matrix — DB-free tests (audience×channel defaults + validation)."""
from types import SimpleNamespace

import pytest

from app.services import notification_matrix as nm


class FakeRule:
    def __init__(self, enabled=True, channels=("email", "dashboard")):
        self.enabled = enabled
        self.enabled_channels = channels


class FakeDB:
    """Stands in for get_rule (patched) + Session.get(rule row)."""

    def __init__(self, row=None):
        self.row = row

    def get(self, model, key):
        return self.row


@pytest.fixture(autouse=True)
def _patch_get_rule(monkeypatch):
    def fake_get_rule(db, ntype):
        return FakeRule()
    monkeypatch.setattr("app.services.notification_config.get_rule", fake_get_rule)


def _row(audience_channels=None, attachments=None):
    return SimpleNamespace(
        audience_channels=audience_channels or {},
        attachments=attachments or [],
    )


class TestDefaultAudiences:
    def test_borrower_default(self):
        assert nm.default_audiences("application_approved") == ("borrower",)

    def test_staff_admin_for_backoffice(self):
        assert nm.default_audiences("bo_loan_assigned") == ("staff", "admin")

    def test_vendor(self):
        assert nm.default_audiences("vendor_loan_assigned") == ("vendor",)


class TestMatrix:
    def test_dashboard_always_on_for_primary_audience(self):
        em = nm.get_event_matrix(FakeDB(), "application_approved")
        assert em.enabled("borrower", "dashboard")
        # dashboard cells are locked
        cell = next(c for c in em.cells
                    if c.audience == "borrower" and c.channel == "dashboard")
        assert cell.locked

    def test_non_primary_audience_off_by_default(self):
        em = nm.get_event_matrix(FakeDB(), "application_approved")
        assert not em.enabled("vendor", "email")
        assert not em.enabled("vendor", "dashboard")

    def test_email_default_from_rule_channels(self):
        em = nm.get_event_matrix(FakeDB(), "application_approved")
        assert em.enabled("borrower", "email")   # email in FakeRule channels
        assert not em.enabled("borrower", "sms")  # sms not in channels

    def test_stored_cell_overrides_default(self):
        db = FakeDB(_row(audience_channels={"borrower": {"sms": True}}))
        em = nm.get_event_matrix(db, "application_approved")
        assert em.enabled("borrower", "sms")

    def test_stored_audience_enables_dashboard(self):
        db = FakeDB(_row(audience_channels={"vendor": {"email": True}}))
        em = nm.get_event_matrix(db, "application_approved")
        assert em.enabled("vendor", "email")
        assert em.enabled("vendor", "dashboard")  # forced on for stored audience

    def test_attachments_passthrough(self):
        db = FakeDB(_row(attachments=["loan_agreement", "privacy_policy"]))
        em = nm.get_event_matrix(db, "application_approved")
        assert em.attachments == ("loan_agreement", "privacy_policy")


class TestAudienceChannelEnabled:
    def test_disabled_event_silences_all_cells(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.notification_config.get_rule",
            lambda db, ntype: FakeRule(enabled=False),
        )
        assert not nm.audience_channel_enabled(
            FakeDB(), "application_approved", "borrower", "dashboard"
        )


class TestValidation:
    def test_valid_update(self):
        assert nm.validate_matrix_update(
            {"borrower": {"email": True, "sms": False}}, ["loan_agreement"]
        ) is None

    def test_unknown_audience(self):
        assert nm.validate_matrix_update({"martians": {"email": True}}, None) is not None

    def test_unknown_channel(self):
        assert nm.validate_matrix_update({"borrower": {"telepathy": True}}, None) is not None

    def test_non_bool_cell(self):
        assert nm.validate_matrix_update({"borrower": {"email": "yes"}}, None) is not None

    def test_dashboard_cannot_be_disabled(self):
        err = nm.validate_matrix_update({"borrower": {"dashboard": False}}, None)
        assert err is not None and "always-on" in err

    def test_unknown_attachment(self):
        assert nm.validate_matrix_update(None, ["mystery_doc"]) is not None
