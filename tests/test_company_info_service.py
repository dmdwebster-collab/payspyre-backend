"""WS-F company-info accessor — DB-free tests (defaults + row merge + context)."""
from types import SimpleNamespace

from app.services import company_info as ci


class FakeDB:
    def __init__(self, row=None):
        self.row = row

    def get(self, model, key):
        return self.row if key == 1 else None


def _row(**kw):
    base = dict(
        legal_name=None, operating_name=None, brand_name=None, lending_type=None,
        logo_ref=None, favicon_ref=None, contacts=[],
    )
    base.update(kw)
    return SimpleNamespace(**base)


class TestDefaults:
    def test_defaults_shape(self):
        d = ci.get_defaults()
        assert d.legal_name == "PaySpyre Financial Inc."
        assert d.operating_name == "PaySpyre Financial"
        assert d.brand_name == "PaySpyre"
        assert d.primary("phone")
        assert d.primary("email") == "support@payspyre.com"

    def test_get_company_info_empty_db_uses_defaults(self):
        info = ci.get_company_info(FakeDB())
        assert info.legal_name == "PaySpyre Financial Inc."


class TestRowMerge:
    def test_partial_row_fills_from_defaults(self):
        info = ci.from_row(_row(legal_name="Acme Lending Inc."))
        assert info.legal_name == "Acme Lending Inc."
        assert info.operating_name == "PaySpyre Financial"  # inherited

    def test_contacts_override(self):
        contacts = [
            {"kind": "phone", "value": "1-800-111", "is_primary": True},
            {"kind": "phone", "value": "1-800-222"},
            {"kind": "email", "value": "hi@acme.test", "is_primary": True},
        ]
        info = ci.from_row(_row(legal_name="Acme", contacts=contacts))
        assert info.primary("phone") == "1-800-111"
        assert info.primary("email") == "hi@acme.test"

    def test_primary_falls_back_to_first_of_kind(self):
        info = ci.from_row(_row(contacts=[{"kind": "phone", "value": "555"}]))
        assert info.primary("phone") == "555"
        assert info.primary("website") is None


class TestContext:
    def test_to_context_keys(self):
        ctx = ci.to_context(ci.get_defaults())
        assert ctx["company_name"] == "PaySpyre Financial Inc."
        assert ctx["company_operating_name"] == "PaySpyre Financial"
        assert ctx["company_brand_name"] == "PaySpyre"
        assert ctx["company_phone"]
        assert ctx["support_email"] == "support@payspyre.com"

    def test_context_override_empty_without_edit(self):
        # No DB row → override is empty so the static render values win.
        assert ci.get_company_context(FakeDB()) == {}

    def test_context_override_when_customized(self):
        db = FakeDB(_row(legal_name="Acme Lending Inc.",
                         contacts=[{"kind": "phone", "value": "1-800-ACME",
                                    "is_primary": True}]))
        ctx = ci.get_company_context(db)
        assert ctx["company_name"] == "Acme Lending Inc."
        assert ctx["company_phone"] == "1-800-ACME"

    def test_context_read_failure_degrades_to_empty(self):
        class ExplodingDB:
            def get(self, *a):
                raise RuntimeError("boom")

        assert ci.get_company_context(ExplodingDB()) == {}
