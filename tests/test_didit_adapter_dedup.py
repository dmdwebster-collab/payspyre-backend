"""Disambiguation guard for the two Didit adapters (no DB, no HTTP).

There used to be two classes both named ``DiditVerificationAdapter``:
  - the session-oriented P4 flow-engine wrapper in ``adapters/didit.py``
  - the real outbound P7.2 adapter in ``adapters/didit_verification.py``

The duplicate name risked the wrong import. The legacy one was renamed to
``DiditSessionVerificationAdapter``; these tests pin the names + wiring so the
ambiguity can't silently come back.
"""
from __future__ import annotations

from app.services.adapters.didit import DiditSessionVerificationAdapter
from app.services.adapters.didit_verification import DiditVerificationAdapter
from app.services.verifications import dispatcher as dispatcher_mod


def test_legacy_module_no_longer_defines_DiditVerificationAdapter():
    """adapters/didit.py must not re-introduce the colliding name."""
    import app.services.adapters.didit as legacy

    assert not hasattr(legacy, "DiditVerificationAdapter")
    assert hasattr(legacy, "DiditSessionVerificationAdapter")


def test_two_adapters_are_distinct_classes():
    assert DiditSessionVerificationAdapter is not DiditVerificationAdapter
    assert DiditSessionVerificationAdapter.__name__ == "DiditSessionVerificationAdapter"
    assert DiditVerificationAdapter.__name__ == "DiditVerificationAdapter"


def test_dispatcher_uses_the_real_outbound_adapter():
    """The dispatcher's ``DiditVerificationAdapter`` is the real outbound one
    (initiate()), not the legacy session wrapper (verify_identity())."""
    assert dispatcher_mod.DiditVerificationAdapter is DiditVerificationAdapter
    assert hasattr(DiditVerificationAdapter, "initiate")
    # Legacy wrapper has no initiate(); it implements the async verify_identity path.
    assert not hasattr(DiditSessionVerificationAdapter, "initiate")
    assert hasattr(DiditSessionVerificationAdapter, "verify_identity")


def test_package_exports_legacy_under_new_name_only():
    import app.services.adapters as adapters_pkg

    assert "DiditSessionVerificationAdapter" in adapters_pkg.__all__
    assert "DiditVerificationAdapter" not in adapters_pkg.__all__
    assert adapters_pkg.DiditSessionVerificationAdapter is DiditSessionVerificationAdapter
