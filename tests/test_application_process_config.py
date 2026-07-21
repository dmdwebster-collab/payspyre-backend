"""WS W2-APPCONFIG — application-process config (DB-free).

Covers: typed-schema defaults & behaviour-preservation (offer expiry 30 / max 3
equal settings.OFFER_*), Dave's dictionary mandates (FT/PT/seasonal employment;
no Employment Insurance / Student income types), strict validation, form-variant
resolution, and the service's effective-config / offer-policy resolution over a
fake session."""
import pytest

from app.core.config import settings
from app.schemas import application_process_config as apc
from app.services import application_process_config as service


class FakeDB:
    """Minimal stand-in for Session.get / add / commit / refresh."""

    def __init__(self, row=None):
        self._row = row
        self.added = []
        self.committed = False

    def get(self, model, key):
        return self._row

    def add(self, obj):
        self.added.append(obj)
        self._row = obj

    def commit(self):
        self.committed = True

    def refresh(self, obj):
        pass


class TestDefaults:
    def test_defaults_preserve_offer_settings(self):
        """The config's offer defaults MUST equal the current settings — the
        behaviour-preservation contract for the offer engine."""
        cfg = apc.ApplicationProcessConfig()
        assert cfg.flow.offer_expiry_days == settings.OFFER_EXPIRY_DAYS
        assert cfg.flow.max_offers == settings.OFFER_MAX_PER_APPLICATION
        assert cfg.flow.offer_expiry_days == 30
        assert cfg.flow.max_offers == 3

    def test_co_applicant_defaults_match_tl(self):
        co = apc.ApplicationProcessConfig().co_applicant
        assert co.enabled is True
        assert co.label == "Co-Borrower"
        assert co.credit_bureau_check is False
        assert co.required is False
        assert co.cc_notifications is True

    def test_default_variant_present(self):
        cfg = apc.ApplicationProcessConfig()
        assert cfg.variant(None).name == "default"
        assert cfg.variant("does-not-exist").name == "default"


class TestDictionaries:
    def test_employment_types_ft_pt_seasonal(self):
        cfg = apc.ApplicationProcessConfig()
        codes = {i.code for i in cfg.dictionaries["employment_types"]}
        assert codes == {"full_time", "part_time", "seasonal"}

    def test_income_types_drop_ei_and_student(self):
        cfg = apc.ApplicationProcessConfig()
        codes = {i.code for i in cfg.dictionaries["income_types"]}
        assert "employment_insurance" not in codes
        assert "student" not in codes
        # sanity: the kept types are present
        assert "employed_full_time" in codes
        assert "self_employed" in codes

    def test_rejection_reasons_have_unique_codes(self):
        cfg = apc.ApplicationProcessConfig()
        codes = [i.code for i in cfg.dictionaries["loan_rejection_reasons"]]
        assert len(codes) == len(set(codes))


class TestParsing:
    def test_none_and_empty_are_defaults(self):
        assert apc.parse_application_process_config(None) == apc.ApplicationProcessConfig()
        assert apc.parse_application_process_config({}) == apc.ApplicationProcessConfig()

    def test_roundtrip(self):
        cfg = apc.ApplicationProcessConfig()
        dumped = cfg.model_dump(mode="json")
        assert apc.parse_application_process_config(dumped) == cfg

    def test_unknown_key_rejected(self):
        with pytest.raises(apc.ApplicationProcessConfigError):
            apc.parse_application_process_config({"flow": {"bogus": 1}})

    def test_bad_offer_expiry_rejected(self):
        with pytest.raises(apc.ApplicationProcessConfigError):
            apc.parse_application_process_config({"flow": {"offer_expiry_days": 0}})

    def test_non_dict_rejected(self):
        with pytest.raises(apc.ApplicationProcessConfigError):
            apc.parse_application_process_config([1, 2, 3])

    def test_variants_must_include_default(self):
        with pytest.raises(apc.ApplicationProcessConfigError):
            apc.parse_application_process_config(
                {"form_variants": [{"name": "short", "label": "Short"}]}
            )

    def test_duplicate_dictionary_codes_rejected(self):
        with pytest.raises(apc.ApplicationProcessConfigError):
            apc.parse_application_process_config(
                {
                    "dictionaries": {
                        "x": [
                            {"code": "A", "title": "a"},
                            {"code": "A", "title": "b"},
                        ]
                    }
                }
            )


class TestService:
    def test_effective_config_defaults_when_no_row(self):
        assert service.get_effective_config(FakeDB(None)) == apc.ApplicationProcessConfig()

    def test_effective_config_defaults_when_db_none(self):
        assert service.get_effective_config(None) == apc.ApplicationProcessConfig()

    def test_offer_policy_defaults_preserve_behavior(self):
        pol = service.effective_offer_policy(FakeDB(None))
        assert pol.expiry_days == settings.OFFER_EXPIRY_DAYS
        assert pol.max_offers == settings.OFFER_MAX_PER_APPLICATION

    def test_offer_policy_reads_edited_row(self):
        cfg = apc.ApplicationProcessConfig()
        cfg.flow.offer_expiry_days = 14
        cfg.flow.max_offers = 5
        row = type("Row", (), {"config": cfg.model_dump(mode="json")})()
        pol = service.effective_offer_policy(FakeDB(row))
        assert pol.expiry_days == 14
        assert pol.max_offers == 5

    def test_invalid_stored_blob_degrades_to_defaults(self):
        row = type("Row", (), {"config": {"flow": {"offer_expiry_days": -1}}})()
        # A bad stored blob must not brick origination — read returns defaults.
        assert service.get_effective_config(FakeDB(row)) == apc.ApplicationProcessConfig()

    def test_save_config_upserts_and_normalizes(self):
        db = FakeDB(None)
        cfg = apc.ApplicationProcessConfig()
        cfg.flow.max_offers = 4
        saved = service.save_config(db, cfg, updated_by="admin-1")
        assert db.committed is True
        assert saved.flow.max_offers == 4
        assert db.added and db.added[0].updated_by == "admin-1"
