"""P0/T4 — the three DEFINITIONAL values that shipped silently WRONG.

These are values that produced plausible-but-incorrect numbers against Dave's
spec. Each test pins the CORRECTED value and, separately, proves the value is
CONFIGURABLE (so Dave can retune it without a deploy) rather than a fresh
hard-coded constant.

    1. Delinquency default threshold  121 DPD  ->  91 DPD  (">120" -> ">90")
    2. DPD ageing vocabulary          current/30/60/90/120plus  and
                                      1-29/30-59/60-89/90plus
                                  ->  good/1-30/31-60/61-90/91plus (ONE set)
    3. Credit-bureau approve cut      680  ->  660   *** CONFIRM WITH DAVE ***

DB-FREE by construction: the policy loaders take a Session but are exercised
here with tiny fakes, so this module runs without Postgres.

    python -m pytest tests/test_p0_definitional_values.py -p no:warnings -q
"""
from dataclasses import replace

import pytest

from app.services import decision_rules as DR
from app.services import delinquency_buckets as D
from app.services import flow_engine
from app.services.metrics import analytics_reports as R


# ---------------------------------------------------------------------------
# Fakes for the integration-settings-backed policy loader
# ---------------------------------------------------------------------------


class _Row:
    def __init__(self, config, enabled=True):
        self.config = config
        self.enabled = enabled


class _FakeSettings:
    """Stands in for app.services.integration_settings.get."""

    def __init__(self, row):
        self._row = row

    def get(self, db, provider):
        assert provider == "delinquency_buckets"
        return self._row


@pytest.fixture
def patch_settings(monkeypatch):
    def _apply(row):
        import app.services.integration_settings as real

        monkeypatch.setattr(real, "get", _FakeSettings(row).get)

    return _apply


# ---------------------------------------------------------------------------
# 1. Default threshold — Dave: "instead of the 120 plus … once they get past
#    pot 90 they would just be quote unquote default."
# ---------------------------------------------------------------------------


class TestDefaultThreshold:
    def test_shipped_default_is_91_not_121(self):
        assert D.DEFAULT_POLICY.default_min_dpd == 91

    @pytest.mark.parametrize(
        "dpd,expected",
        [
            (89, D.BUCKET_POT_60),
            (90, D.BUCKET_POT_90),   # last pot_90 day
            (91, D.BUCKET_DEFAULT),  # ">90 → default"
            (120, D.BUCKET_DEFAULT),  # the OLD rule kept this in pot_90
            (121, D.BUCKET_DEFAULT),
        ],
    )
    def test_ladder_boundaries(self, dpd, expected):
        assert D._dpd_bucket(dpd, D.DEFAULT_POLICY) == expected

    def test_threshold_is_configurable_via_settings(self, patch_settings):
        """Dave can put it back to Turnkey's 121 (or anything else) with a
        settings row — no deploy, no code change."""
        patch_settings(_Row({"default_min_dpd": 121}))
        policy = D.get_policy(object())
        assert policy.default_min_dpd == 121
        assert D._dpd_bucket(120, policy) == D.BUCKET_POT_90
        # ...and every other field keeps its shipped value.
        assert policy.pot_30_min_dpd == 30

    def test_missing_or_disabled_row_yields_shipped_defaults(self, patch_settings):
        patch_settings(None)
        assert D.get_policy(object()) is D.DEFAULT_POLICY
        patch_settings(_Row({"default_min_dpd": 121}, enabled=False))
        assert D.get_policy(object()) is D.DEFAULT_POLICY

    def test_malformed_config_never_breaks_the_read_path(self, patch_settings):
        patch_settings(_Row({"default_min_dpd": "not-a-number"}))
        assert D.get_policy(object()) is D.DEFAULT_POLICY

    def test_none_db_is_pure_defaults(self):
        assert D.get_policy(None) is D.DEFAULT_POLICY


# ---------------------------------------------------------------------------
# 2. DPD ageing vocabulary — Dave's platform-wide `Good / 1-30 / 31-60 /
#    61-90 / >91`, upper bounds INCLUSIVE.
# ---------------------------------------------------------------------------


class TestAgingVocabulary:
    def test_vocabulary_is_daves(self):
        assert D.AGING_BUCKETS == ("1-30", "31-60", "61-90", "91plus")
        assert D.AGING_BUCKETS_WITH_GOOD[0] == "good"
        assert [D.AGING_BUCKET_LABELS[b] for b in D.AGING_BUCKETS_WITH_GOOD] == [
            "Good", "1-30", "31-60", "61-90", ">91",
        ]

    @pytest.mark.parametrize(
        "dpd,expected",
        [
            (-1, "good"), (0, "good"),
            (1, "1-30"), (30, "1-30"),    # 30 used to fall into "30-59"
            (31, "31-60"), (60, "31-60"),  # 60 used to fall into "60-89"
            (61, "61-90"), (90, "61-90"),  # 90 used to fall into "90plus"
            (91, "91plus"), (10_000, "91plus"),
        ],
    )
    def test_inclusive_upper_bounds(self, dpd, expected):
        assert D.aging_bucket(dpd) == expected

    def test_no_120plus_bucket_survives(self):
        """Dave folded 120+ into Default; it must not exist as an ageing row."""
        assert "120plus" not in D.AGING_BUCKETS_WITH_GOOD
        assert "120plus" not in R.COLLECTIONS_BUCKETS
        assert "120plus" not in R.DELINQUENCY_BUCKETS
        assert D.aging_bucket(500) == "91plus"

    def test_reports_share_the_one_vocabulary(self):
        """The collections ageing report and the risk delinquency-performance
        report shipped DIFFERENT boundaries and so disagreed about the same
        loan. They are now the same function."""
        assert R.COLLECTIONS_BUCKETS == R.DELINQUENCY_BUCKETS == list(D.AGING_BUCKETS)
        for dpd in (0, 1, 30, 31, 60, 61, 90, 91, 200):
            assert R.collections_bucket(dpd) == R.days_past_due_bucket(dpd)
            assert R.collections_bucket(dpd) == D.aging_bucket(dpd)

    def test_ml_export_uses_the_same_mapper(self):
        """The training-data export carried a fourth copy of the boundaries."""
        import scripts.ml.export_training_data as ml

        for dpd in (0, 1, 30, 31, 60, 61, 90, 91, 200):
            assert ml.days_past_due_bucket(dpd) == D.aging_bucket(dpd)
        # Its default label follows the corrected threshold too.
        assert ml.derive_labels(status="active", max_dpd=90)["outcome_default"] == 0
        assert ml.derive_labels(status="active", max_dpd=91)["outcome_default"] == 1

    def test_collections_endpoint_uses_the_shared_names(self):
        from app.api.v1.endpoints import admin_collections as AC

        assert AC._BUCKET_NAMES == D.AGING_BUCKETS_WITH_GOOD
        assert AC._bucket_for(30) == "1-30"

    def test_boundaries_are_configurable(self, patch_settings):
        patch_settings(_Row({"aging_1_30_max_dpd": 45, "aging_31_60_max_dpd": 75}))
        policy = D.get_policy(object())
        assert D.aging_bucket(40, policy) == "1-30"
        assert D.aging_bucket(70, policy) == "31-60"
        # Shipped defaults are unaffected by the override object.
        assert D.aging_bucket(40) == "31-60"

    def test_policy_is_a_frozen_value_object(self):
        """No call site can mutate the shared policy out from under another."""
        with pytest.raises(Exception):
            D.DEFAULT_POLICY.default_min_dpd = 5  # type: ignore[misc]
        assert replace(D.DEFAULT_POLICY, default_min_dpd=5).default_min_dpd == 5
        assert D.DEFAULT_POLICY.default_min_dpd == 91


# ---------------------------------------------------------------------------
# 3. Credit-bureau manual-review band — 660 on Dave's screen, 680 in the engine.
#    *** DECISION-PATH: confirm 660 with Dave before production. ***
# ---------------------------------------------------------------------------


class TestBureauScoreBand:
    def test_seeded_approve_cut_is_660(self):
        assert DR.BUREAU_SCORE_DEFAULT_APPROVE_AT == 660
        assert DR.BUREAU_SCORE_DEFAULT_DECLINE_BELOW == 600
        assert DR.DECISION_RULE_REGISTRY["bureau_score"].params == {
            "decline_below": 600,
            "approve_at": 660,
        }

    def test_engine_default_band_is_derived_not_duplicated(self):
        """One source of truth: the engine's default band IS the registry's."""
        assert flow_engine.DEFAULT_MANUAL_REVIEW_BAND == {"min": 600, "max": 659}
        assert flow_engine.DEFAULT_MANUAL_REVIEW_BAND == DR.default_manual_review_band()

    @pytest.mark.parametrize(
        "score,expected",
        [
            (599, "declined"),
            (600, "manual_review"),
            (659, "manual_review"),
            (660, "approved"),   # WAS manual_review under the 680 cut
            (679, "approved"),   # WAS manual_review under the 680 cut
            (680, "approved"),
        ],
    )
    def test_prequalify_matches_the_corrected_band(self, score, expected):
        assert flow_engine.prequalify_score(score, {}) == expected

    def test_660_to_679_is_the_population_that_changed(self):
        """The exact decision delta this correction introduces, pinned so it
        cannot drift silently: scores 660-679 move manual_review -> approved."""
        old_cfg = {"manual_review_band": {"min": 600, "max": 679}}  # the 680 cut
        changed = [
            s for s in range(300, 900)
            if flow_engine.prequalify_score(s, {})
            != flow_engine.prequalify_score(s, old_cfg)
        ]
        assert changed == list(range(660, 680))
        assert all(flow_engine.prequalify_score(s, {}) == "approved" for s in changed)
        assert all(
            flow_engine.prequalify_score(s, old_cfg) == "manual_review" for s in changed
        )

    def test_per_product_snapshot_still_wins(self):
        """Behaviour preservation: products that PIN a band are untouched by
        the default change (Hard Rule #7 — snapshot config is law)."""
        cfg = {"manual_review_band": {"min": 600, "max": 679}}
        assert flow_engine._manual_review_band(cfg) == {"min": 600, "max": 679}
        assert flow_engine.prequalify_score(670, cfg) == "manual_review"

    def test_band_is_admin_configurable_without_a_deploy(self):
        """The decision-rules overlay is the tuning surface; an edit row moves
        the band on the live decision path."""
        class _FakeDB:
            def get(self, _model, key):
                if key == "bureau_score":
                    return type("R", (), {
                        "rule_key": "bureau_score",
                        "enabled": True,
                        "params": {"decline_below": 600, "approve_at": 680},
                        "outcome": None,
                    })()
                return None

        class _Product:
            def __init__(self):
                self.verification_matrix = {"bureau": {}}

        product = _Product()
        out = DR.apply_overlay(_FakeDB(), product)
        assert out.verification_matrix["bureau"]["manual_review_band"] == {
            "min": 600, "max": 679,
        }
        # Back to 680 behaviour, purely by configuration.
        assert flow_engine.prequalify_score(670, out.verification_matrix["bureau"]) == (
            "manual_review"
        )

    def test_no_edit_row_means_no_overlay(self):
        """DECISION-PATH safety: the overlay stays a no-op by default."""
        class _EmptyDB:
            def get(self, _model, _key):
                return None

        product = object()
        assert DR.apply_overlay(_EmptyDB(), product) is product
