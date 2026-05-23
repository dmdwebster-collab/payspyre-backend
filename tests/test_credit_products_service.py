"""Integration tests for Credit Product Service (PR P3)

Tests the service layer for CRUD operations, JSON Schema validation,
platform_events audit trail, and seed data integrity.

All tests run against the live Supabase Session Pooler — no SQLite, no mocks.
"""
import json
import uuid

import pytest
from jsonschema import ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.schemas.credit_products import CreditProductCreate, CreditProductUpdate
from app.services.credit_products import (
    create_credit_product,
    deactivate_credit_product,
    get_credit_product,
    get_credit_product_by_code,
    list_credit_products,
    update_credit_product,
    _validate_verification_matrix,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_MATRIX = {
    "identity": {
        "required": True,
        "methods": ["email_otp", "id_doc_scan"],
        "min_confidence": 0.85,
    },
    "income": {
        "required": True,
        "methods": ["bank_link", "t4"],
        "require_bank_link": False,
        "min_stated_income_cents": 4000000,
    },
    "bureau": {
        "soft_pull_required": True,
        "hard_pull_required": True,
        "min_score": 640,
        "max_score_age_days": 90,
    },
    "affordability": {
        "max_dti": 0.42,
        "min_payment_to_income_ratio": 0.20,
        "max_loan_to_income_ratio": 2.5,
    },
}

_VALID_PRICING = {
    "term_options": [24, 36, 48],
    "apr_range": [8.99, 24.99],
    "origination_fee_pct": 0.02,
}


def _make_create_data(suffix: str | None = None) -> CreditProductCreate:
    s = suffix or uuid.uuid4().hex[:8]
    return CreditProductCreate(
        code=f"test_product_{s}",
        name=f"Test Product {s}",
        vertical="dental",
        status="active",
        min_amount_cents=500000,
        max_amount_cents=5000000,
        currency="CAD",
        verification_matrix=_VALID_MATRIX,
        decision_ruleset=f"test_ruleset_{s}.yaml",
        pricing_config=_VALID_PRICING,
        funding_source="payspyre_capital",
    )


# ---------------------------------------------------------------------------
# CRUD happy paths
# ---------------------------------------------------------------------------


class TestCreateCreditProduct:
    def test_create_returns_product_with_id(self, db_session: Session):
        product = create_credit_product(db_session, _make_create_data())
        assert product.id is not None
        assert product.code.startswith("test_product_")
        assert product.status == "active"
        assert product.version == 1

    def test_create_sets_all_required_fields(self, db_session: Session):
        data = _make_create_data()
        product = create_credit_product(db_session, data)
        assert product.code == data.code
        assert product.name == data.name
        assert product.vertical == "dental"
        assert product.min_amount_cents == 500000
        assert product.max_amount_cents == 5000000
        assert product.currency == "CAD"
        assert product.decision_ruleset == data.decision_ruleset
        assert product.funding_source == "payspyre_capital"

    def test_create_stores_verification_matrix(self, db_session: Session):
        product = create_credit_product(db_session, _make_create_data())
        assert product.verification_matrix["bureau"]["min_score"] == 640
        assert product.verification_matrix["identity"]["required"] is True


class TestGetCreditProduct:
    def test_get_by_id_returns_product(self, db_session: Session):
        created = create_credit_product(db_session, _make_create_data())
        fetched = get_credit_product(db_session, created.id)
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.code == created.code

    def test_get_by_id_returns_none_for_missing(self, db_session: Session):
        result = get_credit_product(db_session, uuid.uuid4())
        assert result is None

    def test_get_by_code_returns_product(self, db_session: Session):
        created = create_credit_product(db_session, _make_create_data())
        fetched = get_credit_product_by_code(db_session, created.code)
        assert fetched is not None
        assert fetched.id == created.id

    def test_get_by_code_returns_none_for_missing(self, db_session: Session):
        result = get_credit_product_by_code(db_session, "nonexistent_code_xyz")
        assert result is None


class TestUpdateCreditProduct:
    def test_update_name_and_decision_ruleset(self, db_session: Session):
        product = create_credit_product(db_session, _make_create_data())
        updated = update_credit_product(
            db_session,
            product.id,
            CreditProductUpdate(name="Updated Name", decision_ruleset="new_ruleset.yaml"),
        )
        assert updated.name == "Updated Name"
        assert updated.decision_ruleset == "new_ruleset.yaml"

    def test_update_bumps_version(self, db_session: Session):
        product = create_credit_product(db_session, _make_create_data())
        assert product.version == 1
        updated = update_credit_product(
            db_session, product.id, CreditProductUpdate(name="New Name")
        )
        assert updated.version == 2

    def test_update_nonexistent_raises_error(self, db_session: Session):
        with pytest.raises(ValueError, match="not found"):
            update_credit_product(db_session, uuid.uuid4(), CreditProductUpdate(name="x"))


class TestListCreditProducts:
    def test_list_active_only_excludes_draft(self, db_session: Session):
        suffix = uuid.uuid4().hex[:8]
        active_data = _make_create_data(f"active_{suffix}")
        active_data = CreditProductCreate(**{**active_data.model_dump(), "status": "active"})

        draft_data = _make_create_data(f"draft_{suffix}")
        draft_data = CreditProductCreate(**{**draft_data.model_dump(), "status": "draft"})

        active = create_credit_product(db_session, active_data)
        draft = create_credit_product(db_session, draft_data)

        results = list_credit_products(db_session, active_only=True)
        result_ids = {p.id for p in results}

        assert active.id in result_ids
        assert draft.id not in result_ids

    def test_list_all_includes_draft(self, db_session: Session):
        suffix = uuid.uuid4().hex[:8]
        draft_data = _make_create_data(f"listall_{suffix}")
        draft_data = CreditProductCreate(**{**draft_data.model_dump(), "status": "draft"})
        draft = create_credit_product(db_session, draft_data)

        results = list_credit_products(db_session, active_only=False)
        result_ids = {p.id for p in results}
        assert draft.id in result_ids


class TestDeactivateCreditProduct:
    def test_deactivate_sets_archived_status(self, db_session: Session):
        product = create_credit_product(db_session, _make_create_data())
        assert product.status == "active"

        deactivated = deactivate_credit_product(db_session, product.id)
        assert deactivated.status == "archived"

    def test_deactivate_nonexistent_raises_error(self, db_session: Session):
        with pytest.raises(ValueError, match="not found"):
            deactivate_credit_product(db_session, uuid.uuid4())

    def test_deactivated_product_excluded_from_active_list(self, db_session: Session):
        product = create_credit_product(db_session, _make_create_data())
        deactivate_credit_product(db_session, product.id)

        results = list_credit_products(db_session, active_only=True)
        assert product.id not in {p.id for p in results}


# ---------------------------------------------------------------------------
# Duplicate code rejection
# ---------------------------------------------------------------------------


class TestDuplicateCodeRejection:
    def test_duplicate_code_raises_value_error(self, db_session: Session):
        data = _make_create_data()
        create_credit_product(db_session, data)

        with pytest.raises(ValueError, match="already exists"):
            create_credit_product(db_session, data)


# ---------------------------------------------------------------------------
# JSON Schema validation — one rejection per major block
# ---------------------------------------------------------------------------


class TestVerificationMatrixValidation:
    def test_valid_matrix_does_not_raise(self):
        _validate_verification_matrix(_VALID_MATRIX)  # must not raise

    def test_rejects_missing_identity_block(self):
        bad = {k: v for k, v in _VALID_MATRIX.items() if k != "identity"}
        with pytest.raises(ValidationError):
            _validate_verification_matrix(bad)

    def test_rejects_missing_income_block(self):
        bad = {k: v for k, v in _VALID_MATRIX.items() if k != "income"}
        with pytest.raises(ValidationError):
            _validate_verification_matrix(bad)

    def test_rejects_missing_bureau_block(self):
        bad = {k: v for k, v in _VALID_MATRIX.items() if k != "bureau"}
        with pytest.raises(ValidationError):
            _validate_verification_matrix(bad)

    def test_rejects_missing_affordability_block(self):
        bad = {k: v for k, v in _VALID_MATRIX.items() if k != "affordability"}
        with pytest.raises(ValidationError):
            _validate_verification_matrix(bad)

    def test_rejects_invalid_bureau_score_threshold_type(self):
        import copy
        bad = copy.deepcopy(_VALID_MATRIX)
        bad["bureau"]["min_score"] = "not_an_integer"
        with pytest.raises(ValidationError):
            _validate_verification_matrix(bad)

    def test_rejects_invalid_max_dti_out_of_range(self):
        import copy
        bad = copy.deepcopy(_VALID_MATRIX)
        bad["affordability"]["max_dti"] = 1.5
        with pytest.raises(ValidationError):
            _validate_verification_matrix(bad)

    def test_rejects_unknown_identity_method(self):
        import copy
        bad = copy.deepcopy(_VALID_MATRIX)
        bad["identity"]["methods"] = ["email_otp", "unknown_method"]
        with pytest.raises(ValidationError):
            _validate_verification_matrix(bad)

    def test_rejects_additional_properties_in_bureau(self):
        import copy
        bad = copy.deepcopy(_VALID_MATRIX)
        bad["bureau"]["unknown_field"] = "should_not_be_here"
        with pytest.raises(ValidationError):
            _validate_verification_matrix(bad)

    def test_create_rejects_invalid_matrix_before_db_write(self, db_session: Session):
        """Validation must fire before the DB write — no partial row on failure."""
        import copy
        data = _make_create_data()
        bad_matrix = copy.deepcopy(_VALID_MATRIX)
        del bad_matrix["bureau"]
        invalid_data = CreditProductCreate(**{**data.model_dump(), "verification_matrix": bad_matrix})

        with pytest.raises(ValidationError):
            create_credit_product(db_session, invalid_data)

        # Confirm no row was written
        result = get_credit_product_by_code(db_session, data.code)
        assert result is None


# ---------------------------------------------------------------------------
# Seed data integrity
# ---------------------------------------------------------------------------


class TestSeedDataIntegrity:
    def test_dental_full_arch_v1_exists(self, db_session: Session):
        """After migration 022, dental_full_arch_v1 must be present in the DB."""
        product = get_credit_product_by_code(db_session, "dental_full_arch_v1")
        assert product is not None, "dental_full_arch_v1 seed row missing — run 'alembic upgrade head'"
        assert product.name == "Dental Full Arch v1"
        assert product.status == "active"
        assert product.vertical == "dental"

    def test_dental_full_arch_v1_matrix_validates_against_schema(self, db_session: Session):
        """Seed row verification_matrix must pass JSON Schema validation."""
        product = get_credit_product_by_code(db_session, "dental_full_arch_v1")
        assert product is not None
        _validate_verification_matrix(product.verification_matrix)

    def test_dental_full_arch_v1_amount_range_is_reasonable(self, db_session: Session):
        """Full-arch product must allow loans in the $15k–$80k CAD range."""
        product = get_credit_product_by_code(db_session, "dental_full_arch_v1")
        assert product is not None
        assert product.min_amount_cents >= 1_000_000, "min should be at least $10k CAD"
        assert product.max_amount_cents >= 5_000_000, "max should be at least $50k CAD"
        assert product.currency == "CAD"


# ---------------------------------------------------------------------------
# platform_events audit trail
# ---------------------------------------------------------------------------


class TestPlatformEventsAuditTrail:
    def test_create_logs_credit_product_created_event(self, db_session: Session):
        product = create_credit_product(db_session, _make_create_data())

        row = db_session.execute(
            text("""
                SELECT event_type, actor, payload
                FROM platform_events
                WHERE event_type = 'credit_product.created'
                  AND payload->>'product_id' = :pid
            """),
            {"pid": str(product.id)},
        ).fetchone()

        assert row is not None, "credit_product.created event not found in platform_events"
        assert row[1] == "system"
        assert row[2]["code"] == product.code

    def test_update_logs_credit_product_updated_event(self, db_session: Session):
        product = create_credit_product(db_session, _make_create_data())
        update_credit_product(db_session, product.id, CreditProductUpdate(name="Renamed"))

        row = db_session.execute(
            text("""
                SELECT event_type, payload
                FROM platform_events
                WHERE event_type = 'credit_product.updated'
                  AND payload->>'product_id' = :pid
            """),
            {"pid": str(product.id)},
        ).fetchone()

        assert row is not None, "credit_product.updated event not found in platform_events"
        assert "name" in row[1]["fields_updated"]

    def test_deactivate_logs_credit_product_deactivated_event(self, db_session: Session):
        product = create_credit_product(db_session, _make_create_data())
        deactivate_credit_product(db_session, product.id)

        row = db_session.execute(
            text("""
                SELECT event_type, actor, payload
                FROM platform_events
                WHERE event_type = 'credit_product.deactivated'
                  AND payload->>'product_id' = :pid
            """),
            {"pid": str(product.id)},
        ).fetchone()

        assert row is not None, "credit_product.deactivated event not found in platform_events"
        assert row[1] == "system"
