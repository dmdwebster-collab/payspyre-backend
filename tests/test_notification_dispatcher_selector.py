"""Tests for the ``get_notification_dispatcher`` flag-based selector — P7.4.

Mirrors ``tests/test_verification_dispatcher.py`` from P7.2 — exercises the
binary dispatch in ``app/api/applicant/v1/deps.py:get_notification_dispatcher``.
"""
from sqlalchemy.orm import Session

from app.api.applicant.v1.deps import get_notification_dispatcher
from app.core.config import settings
from app.services.mock_notification_dispatcher import MockNotificationDispatcher
from app.services.real_notification_dispatcher import RealNotificationDispatcher


def _patch_real(monkeypatch):
    monkeypatch.setattr(settings, "USE_REAL_NOTIFICATIONS", True)
    # Production validator only runs when ENVIRONMENT='production'; tests stay
    # 'development', so empty creds remain acceptable for selection itself.


class TestFlagOff:
    def test_returns_mock_by_default(self, db_session: Session):
        d = get_notification_dispatcher(db_session)
        assert isinstance(d, MockNotificationDispatcher)

    def test_returns_mock_when_flag_explicitly_false(
        self, db_session: Session, monkeypatch
    ):
        monkeypatch.setattr(settings, "USE_REAL_NOTIFICATIONS", False)
        assert isinstance(get_notification_dispatcher(db_session), MockNotificationDispatcher)


class TestFlagOn:
    def test_returns_real_when_flag_true(self, db_session: Session, monkeypatch):
        _patch_real(monkeypatch)
        assert isinstance(get_notification_dispatcher(db_session), RealNotificationDispatcher)

    def test_real_dispatcher_holds_supplied_db(self, db_session: Session, monkeypatch):
        _patch_real(monkeypatch)
        d = get_notification_dispatcher(db_session)
        assert d.db is db_session


class TestDispatcherContract:
    """Both dispatchers must expose ``send_magic_link`` with the same signature
    (duck-typed in PatientAuthService). This regression-guards the contract."""

    def test_both_have_send_magic_link_method(self):
        assert callable(getattr(MockNotificationDispatcher, "send_magic_link", None))
        assert callable(getattr(RealNotificationDispatcher, "send_magic_link", None))

    def test_send_magic_link_signature_matches(self):
        import inspect
        mock_sig = inspect.signature(MockNotificationDispatcher.send_magic_link)
        real_sig = inspect.signature(RealNotificationDispatcher.send_magic_link)
        assert list(mock_sig.parameters.keys()) == list(real_sig.parameters.keys())
