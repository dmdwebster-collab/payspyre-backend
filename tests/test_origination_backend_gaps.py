"""Four backend gaps the Originations UI was compromising around (2026-07-28).

The frontend rebuild implements the owner's written Originations spec, but three
of its requirements could not be honoured because the backend refused them, and
a fourth was specified but never built. He objects — rightly — to requested
functionality being silently dropped, so each gap gets a test that fails if the
old behaviour comes back:

GAP 1  Payment Frequency was a mandatory origination field that the create
       endpoint had no parameter for. The user picked it, it drove the quote and
       the schedule they were shown, and then it was discarded — even though
       ``platform_credit_applications.preferred_payment_frequency`` already
       existed. Now: accepted, validated against the PRODUCT's allowed set by
       the ONE shared validator, persisted (normalised) and echoed back.

GAP 2  First Payment Date was gated behind a ``use_custom_first_due_date``
       checkbox the spec REMOVES. The date now stands alone, bounded by the
       product's first-payment window. (Payload-level cases live in
       ``test_admin_origination_gaps.TestFinanceTermsPayload``; the window
       enforcement is here, because it needs a real product.)

GAP 3  ``GET /admin/profile-schema`` still emitted the old field order and the
       old "Monthly Mortgage Payment" label, so the applicant-facing journey
       showed the superseded wording. Fixed in the registry, which is the only
       source — the frontend's presentation-layer overrides can now go.

GAP 4  "The Application Number becomes the Loan ID." No loan exists before
       activation, so the agreement the borrower SIGNED rendered
       ``[NOT AVAILABLE: LoanId]``. Now the application number is minted with
       the application, prints as the Loan ID, and is inherited by the loan at
       activation — so the signed document and the live loan agree.
"""
from datetime import date, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.api.v1.endpoints.admin_customer_profiles import (
    ApplicationFromProfileRequest,
    create_application_from_profile,
    get_profile_schema,
)
from app.models.platform.credit_application import PlatformCreditApplication
from app.models.platform.credit_product import PlatformCreditProduct
from app.services import customer_profile as profiles
from app.services import customer_profile_schema as schema

# The product's first-payment window: 10..40 days after the start date.
_FIRST_DUE_MIN_DAYS = 10
_FIRST_DUE_MAX_DAYS = 40

_PRICING = {
    "schema_version": 1,
    "interest": {
        "annual_rate_bps": 1200,
        "min_rate_bps": 900,
        "max_rate_bps": 2400,
        "rate_edit_roles": ["admin"],
    },
    # Deliberately NOT the full set: "weekly" must be refused by this product.
    "payment_frequencies": ["monthly", "bi_weekly"],
    "term_min_months": 12,
    "term_max_months": 60,
    "default_term_months": 24,
    "fees": [],
}

_POLICY = {
    "schema_version": 1,
    "due_dates": {
        "use_change_start_date": True,
        "default_start_shift_days": 0,
        "use_change_first_due_date": True,
        "first_due_min_days": _FIRST_DUE_MIN_DAYS,
        "first_due_max_days": _FIRST_DUE_MAX_DAYS,
    },
}


@pytest.fixture
def gap_product(db_session):
    product = PlatformCreditProduct(
        code=f"gaps_{uuid4().hex[:10]}",
        name="Origination Gaps Test Product",
        vertical="dental",
        status="active",
        min_amount_cents=100_000,
        max_amount_cents=5_000_000,
        currency="CAD",
        verification_matrix={},
        decision_ruleset="dental_full_arch_v1.yaml",
        pricing_config=_PRICING,
        policy_config=_POLICY,
        funding_source="payspyre_capital",
        version=1,
    )
    db_session.add(product)
    db_session.commit()
    return product


@pytest.fixture
def gap_profile(db_session):
    return profiles.create_borrower(
        db_session,
        values={
            "personal": {
                "first_name": "Rowan",
                "last_name": "Alvarez",
                "date_of_birth": "1988-04-11",
                "citizenship": "canadian",
                "education": "college_university",
            },
            "contact": {
                "email": f"rowan.{uuid4().hex[:8]}@example.ca",
                "main_phone": "2505558822",
            },
            "current_address": {
                "street_address": "12 Water St",
                "city": "Kelowna",
                "province": "BC",
                "postal_code": "V1Y6N2",
                "residential_status": "rent",
                "resided_since": (date.today() - timedelta(days=900)).isoformat(),
                "monthly_rent": "1450.00",
            },
            "primary_income": {
                "income_type": "employed_full_time",
                "net_monthly_income": "6100.00",
                "employer_name": "Okanagan Dental Group",
            },
        },
        actor="staff-gaps",
        source="staff",
    )


_USER = SimpleNamespace(id="staff-gaps", email="staff@payspyre.com", roles=[])


def _create(db_session, profile, product, **over):
    """Call the create-from-profile endpoint with sane defaults."""
    start = date.today() + timedelta(days=3)
    body = {
        "credit_product_id": product.id,
        "requested_amount_cents": 500_000,
        "requested_term_months": 24,
        "start_date": start,
        "first_due_date": start + timedelta(days=_FIRST_DUE_MIN_DAYS + 5),
    }
    body.update(over)
    return create_application_from_profile(
        profile.id,
        ApplicationFromProfileRequest(**body),
        db=db_session,
        user=_USER,
    )


def _application(db_session, response) -> PlatformCreditApplication:
    return (
        db_session.query(PlatformCreditApplication)
        .filter(PlatformCreditApplication.id == response["application_id"])
        .one()
    )


# ===========================================================================
# GAP 1 — Payment Frequency survives the create call
# ===========================================================================


class TestPaymentFrequencyIsNotDropped:
    def test_frequency_is_persisted_and_echoed(self, db_session, gap_profile, gap_product):
        response = _create(
            db_session, gap_profile, gap_product, payment_frequency="bi_weekly"
        )
        # Echoed to the caller...
        assert response["payment_frequency"] == "bi_weekly"
        # ...AND actually written to the column that already existed and was
        # being ignored. This is the whole gap: the value used to vanish.
        assert (
            _application(db_session, response).preferred_payment_frequency
            == "bi_weekly"
        )

    def test_an_alias_spelling_is_stored_normalised(
        self, db_session, gap_profile, gap_product
    ):
        """"bi-weekly" is a legal input spelling; only one spelling is stored."""
        response = _create(
            db_session, gap_profile, gap_product, payment_frequency="bi-weekly"
        )
        assert response["payment_frequency"] == "bi_weekly"

    def test_a_frequency_the_product_does_not_offer_is_refused(
        self, db_session, gap_profile, gap_product
    ):
        """Validated by the SHARED validator — no second implementation.

        The product offers monthly + bi-weekly only, so weekly must 422 with the
        same ``frequency_not_offered`` code the quote endpoint produces.
        """
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            _create(db_session, gap_profile, gap_product, payment_frequency="weekly")
        assert exc.value.status_code == 422
        assert "frequency_not_offered" in str(exc.value.detail)

    def test_omitting_the_frequency_still_works(
        self, db_session, gap_profile, gap_product
    ):
        """Older API callers never sent it; they must not start failing."""
        response = _create(db_session, gap_profile, gap_product)
        assert response["payment_frequency"] is None

    def test_frequency_reaches_the_offer_the_agreement_renders_from(
        self, db_session, gap_profile, gap_product
    ):
        """GAP 1, downstream leg.

        ``loan_offers`` hard-coded ``payment_frequency="monthly"`` on every offer
        it created, and the loan agreement renders its Repayment Period from the
        ACCEPTED OFFER — so the document contradicted the frequency the borrower
        chose. The offer must now inherit the application's choice.
        """
        from app.services import loan_offers

        response = _create(
            db_session, gap_profile, gap_product, payment_frequency="bi_weekly"
        )
        application = _application(db_session, response)
        application.status = "under_review"
        db_session.commit()

        offers = loan_offers.create_offers(
            db_session,
            application,
            [
                loan_offers.OfferSpec(
                    amount_cents=500_000,
                    term_months=24,
                    annual_rate_bps=1200,
                    start_date=application.loan_start_date,
                    first_due_date=application.first_due_date,
                )
            ],
            actor="staff-gaps",
        )
        assert [o.payment_frequency for o in offers] == ["bi_weekly"]

    def test_an_application_without_a_preference_still_gets_monthly_offers(
        self, db_session, gap_profile, gap_product
    ):
        """Every pre-existing row has no preference — behaviour must not move."""
        from app.services import loan_offers

        response = _create(db_session, gap_profile, gap_product)
        application = _application(db_session, response)
        application.status = "under_review"
        db_session.commit()

        offers = loan_offers.create_offers(
            db_session,
            application,
            [
                loan_offers.OfferSpec(
                    amount_cents=500_000,
                    term_months=24,
                    annual_rate_bps=1200,
                    start_date=application.loan_start_date,
                    first_due_date=application.first_due_date,
                )
            ],
            actor="staff-gaps",
        )
        assert [o.payment_frequency for o in offers] == ["monthly"]


# ===========================================================================
# GAP 2 — First Payment Date without the removed checkbox
# ===========================================================================


class TestFirstPaymentDateWindow:
    def test_a_date_inside_the_window_is_accepted_with_no_checkbox(
        self, db_session, gap_profile, gap_product
    ):
        start = date.today() + timedelta(days=3)
        response = _create(
            db_session,
            gap_profile,
            gap_product,
            start_date=start,
            first_due_date=start + timedelta(days=_FIRST_DUE_MIN_DAYS),
        )
        assert response["first_due_date"] == start + timedelta(
            days=_FIRST_DUE_MIN_DAYS
        )

    @pytest.mark.parametrize(
        "offset_days",
        [_FIRST_DUE_MIN_DAYS - 1, _FIRST_DUE_MAX_DAYS + 1],
        ids=["before_the_window", "after_the_window"],
    )
    def test_a_date_outside_the_product_window_is_refused(
        self, db_session, gap_profile, gap_product, offset_days
    ):
        """Dropping the checkbox must NOT drop the bound.

        The product's first-payment min/max offsets are what the form's date
        picker is bounded by; the server enforces the same window so posting
        straight to the API cannot bypass it.
        """
        from fastapi import HTTPException

        start = date.today() + timedelta(days=3)
        with pytest.raises(HTTPException) as exc:
            _create(
                db_session,
                gap_profile,
                gap_product,
                start_date=start,
                first_due_date=start + timedelta(days=offset_days),
            )
        assert exc.value.status_code == 422
        assert "first_payment_date_out_of_window" in str(exc.value.detail)

    def test_the_legacy_checkbox_changes_nothing(
        self, db_session, gap_profile, gap_product
    ):
        """Accepted for backward compatibility, but it no longer gates anything."""
        start = date.today() + timedelta(days=3)
        due = start + timedelta(days=_FIRST_DUE_MIN_DAYS + 2)
        with_flag = _create(
            db_session,
            gap_profile,
            gap_product,
            start_date=start,
            first_due_date=due,
            use_custom_first_due_date=True,
        )
        without_flag = _create(
            db_session,
            gap_profile,
            gap_product,
            start_date=start,
            first_due_date=due,
        )
        assert with_flag["first_due_date"] == without_flag["first_due_date"] == due


# ===========================================================================
# GAP 3 — the registry emits the owner's order and label
# ===========================================================================


class TestProfileSchemaOrderAndLabel:
    @staticmethod
    def _current_address_keys() -> list[str]:
        payload = get_profile_schema()
        for block in payload["blocks"]:
            if block["block"] == "current_address":
                return [f["key"] for f in block["fields"]]
        raise AssertionError("current_address block missing from the schema payload")

    def test_residential_status_sits_between_resided_since_and_housing_cost(self):
        """The owner's explicit reorder — asserted on the ENDPOINT's payload.

        The frontend had to re-order these itself; with this the override goes,
        and the applicant-facing journey (which renders from the same registry)
        agrees with the back office.
        """
        keys = self._current_address_keys()
        assert (
            keys.index("resided_since")
            < keys.index("residential_status")
            < keys.index("monthly_mortgage_payment")
        )
        # And it is the row IMMEDIATELY after "Resided at address since".
        assert keys[keys.index("resided_since") + 1] == "residential_status"

    def test_the_housing_cost_pair_shares_the_new_label(self):
        payload = get_profile_schema()
        labels = {
            f["key"]: f["label"]
            for block in payload["blocks"]
            for f in block["fields"]
            if f["key"] in ("monthly_mortgage_payment", "monthly_rent")
        }
        assert labels == {
            "monthly_mortgage_payment": "Monthly Housing Costs (Rent / Mortgage)",
            "monthly_rent": "Monthly Housing Costs (Rent / Mortgage)",
        }
        # The superseded wording is gone from the registry entirely.
        all_labels = [
            f["label"] for block in payload["blocks"] for f in block["fields"]
        ]
        assert "Monthly Mortgage Payment" not in all_labels
        assert "Monthly Rent" not in all_labels

    def test_the_pair_is_declared_one_logical_field(self):
        """APPROACH (a): two storage keys, one presented field.

        Both keys are kept — a mortgage payment and a rent payment are different
        facts for underwriting, and collapsing them would have to migrate stored
        values — so they are tied together by ``field_group`` instead. A consumer
        renders the group as ONE row and lets residential status pick the half.
        """
        payload = get_profile_schema()
        grouped = {
            f["key"]: f["field_group"]
            for block in payload["blocks"]
            for f in block["fields"]
            if f.get("field_group") == schema.HOUSING_COST_GROUP
        }
        assert set(grouped) == {"monthly_mortgage_payment", "monthly_rent"}

    @pytest.mark.parametrize(
        "status,applicable",
        [
            ("rent", {"monthly_rent"}),
            ("own_detached", {"monthly_mortgage_payment"}),
            ("own_mobile_rents_land", {"monthly_mortgage_payment"}),
            # Neither half applies — the group renders nothing at all.
            ("living_with_parents", set()),
        ],
    )
    def test_residential_status_picks_at_most_one_half(self, status, applicable):
        values = {"current_address": {"residential_status": status}}
        specs = {
            spec.key: spec
            for spec in schema.block_spec(schema.ProfileBlock.CURRENT_ADDRESS).fields
            if spec.field_group == schema.HOUSING_COST_GROUP
        }
        visible = {
            key for key, spec in specs.items() if schema.is_field_visible(spec, values)
        }
        assert visible == applicable


# ===========================================================================
# GAP 4 — the Application Number IS the Loan ID
# ===========================================================================


class TestApplicationNumberIsTheLoanId:
    def test_every_application_is_born_with_a_number(
        self, db_session, gap_profile, gap_product
    ):
        """Minted by a column DEFAULT, so no create path can forget it."""
        application = _application(
            db_session, _create(db_session, gap_profile, gap_product)
        )
        assert application.application_number
        assert application.application_number.isdigit()

    def test_numbers_are_unique_across_applications(
        self, db_session, gap_profile, gap_product
    ):
        first = _application(db_session, _create(db_session, gap_profile, gap_product))
        second = _application(db_session, _create(db_session, gap_profile, gap_product))
        assert first.application_number != second.application_number

    def test_the_preview_renders_a_real_loan_id_before_any_loan_exists(
        self, db_session, gap_profile, gap_product
    ):
        """The gap itself: this used to render ``[NOT AVAILABLE: LoanId]``.

        The borrower was being asked to sign a loan agreement that did not name
        the loan it was about.
        """
        from app.services.application_agreement_preview import (
            NOT_AVAILABLE_FMT,
            build_preview,
        )

        application = _application(
            db_session, _create(db_session, gap_profile, gap_product)
        )
        result = build_preview(application, product=gap_product, loan=None)

        assert result.merge_data["LoanId"] == application.application_number
        assert NOT_AVAILABLE_FMT.format(field="LoanId") not in result.html
        assert "LoanId" not in {n.field for n in result.missing_fields}

    def test_the_activated_loan_carries_the_same_identifier(
        self, db_session, gap_profile, gap_product
    ):
        """Identity, end to end.

        ``platform_loans.id`` is NOT set to the application's id — two tables'
        primary keys must stay distinct, or an id mix-up silently "works" across
        the money path. Instead the loan inherits the application NUMBER into
        ``loan_number``, so the signed agreement and the live loan name the same
        thing while remaining separate rows.
        """
        from app.services.loan_servicing import create_loan_from_application

        application = _application(
            db_session, _create(db_session, gap_profile, gap_product)
        )
        number = application.application_number
        application.status = "approved"
        db_session.commit()

        loan = create_loan_from_application(db_session, application)

        assert loan.loan_number == number
        assert loan.id != application.id  # distinct rows, one shared identifier

    def test_the_signed_agreement_and_the_booked_loan_agree(
        self, db_session, gap_profile, gap_product
    ):
        """The point of the whole change: sign first, activate later, same ID."""
        from app.services.application_agreement_preview import build_preview
        from app.services.loan_servicing import create_loan_from_application

        application = _application(
            db_session, _create(db_session, gap_profile, gap_product)
        )
        signed_id = build_preview(
            application, product=gap_product, loan=None
        ).merge_data["LoanId"]

        application.status = "approved"
        db_session.commit()
        loan = create_loan_from_application(db_session, application)

        after = build_preview(application, product=gap_product, loan=loan)
        assert after.merge_data["LoanId"] == signed_id == loan.loan_number

    def test_contract_date_populates_on_signature_without_a_loan(
        self, db_session, gap_profile, gap_product
    ):
        """ContractDate revisited.

        It is the SIGNATURE date, and under the activation rework the borrower
        signs on the APPLICATION — before any loan exists. So it resolves
        pre-activation too; it is blank only while the file is unsigned.
        """
        from datetime import datetime, timezone

        from app.services.application_agreement_preview import build_preview

        application = _application(
            db_session, _create(db_session, gap_profile, gap_product)
        )
        unsigned = build_preview(application, product=gap_product, loan=None)
        assert "ContractDate" in {n.field for n in unsigned.missing_fields}

        application.agreement_status = "signed"
        application.agreement_signed_at = datetime(2026, 8, 4, tzinfo=timezone.utc)
        db_session.commit()

        signed = build_preview(application, product=gap_product, loan=None)
        assert signed.merge_data["ContractDate"] == "2026-08-04"
        assert "ContractDate" not in {n.field for n in signed.missing_fields}
        # ...and the Loan ID is there too, so the signed document is complete.
        assert signed.merge_data["LoanId"] == application.application_number
