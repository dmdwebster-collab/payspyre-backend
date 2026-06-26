"""CASL marketing consent must be a registered but OPTIONAL purpose — never
bundled into the required underwriting consents (PIPEDA anti-bundling)."""
from app.services import consent_service
from app.services.flow_orchestrator import _CONSENT_ORDER


def test_marketing_consent_is_registered_and_versioned():
    # registered + loadable, so it can be granted with a versioned, auditable record
    ct = consent_service.get_active_consent_text("marketing_communications")
    assert ct.purpose == "marketing_communications"
    assert ct.version == "v1_2026-06"


def test_marketing_consent_is_not_a_required_underwriting_consent():
    # never part of the required/integral consent ordering — it stays separate and
    # optional (CRTC opt-in + PIPEDA anti-bundling), so the flow never gates on it
    assert "marketing_communications" not in _CONSENT_ORDER
