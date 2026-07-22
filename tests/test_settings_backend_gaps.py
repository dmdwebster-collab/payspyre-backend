"""DB-free unit tests for the three backend-blocked Settings leaves.

Gap 1 (Accounts → Users)   — the workplace-permission catalogue + the additive,
                             behaviour-preserving direct-grant auth path.
Gap 2 (Product config)     — schedule-building fields, allocation ordering and
                             the money-path/engine-consistency guards.
Gap 3 (Integrations depth) — typed Flinks / Equifax behaviour config, and the
                             invariant that secrets stay out of it.

No database is touched: everything here exercises pure schema/dataclass code or
tiny stand-in objects. Endpoint wiring is covered by the existing DB-backed
suites (tests/test_admin_config.py, tests/test_credit_products_api.py).
"""
from __future__ import annotations

import pytest

from app.core.auth import user_has_direct_grant, user_has_permission
from app.schemas.integration_config import (
    PROVIDER_CONFIG_SCHEMAS,
    SECRET_KEYS,
    BankVerificationTiming,
    BureauEnvironment,
    EquifaxConfig,
    FlinksConfig,
    IntegrationConfigError,
    resolve_provider_config,
    validate_provider_config,
)
from app.schemas.product_policy_config import (
    ALLOCATION_CATEGORIES,
    DEFAULT_ACCRUAL_PRIORITY,
    DEFAULT_REPAYMENT_PRIORITY,
    CalculationBasis,
    LoanPhase,
    LoanType,
    LoanTypeChangeError,
    ProductPolicyConfig,
    ProductPolicyConfigError,
    assert_loan_type_change_allowed,
    parse_product_policy_config,
)
from app.services import staff_accounts


# ---------------------------------------------------------------------------
# Tiny stand-ins for the ORM objects the auth helpers walk.
# ---------------------------------------------------------------------------


class _Perm:
    def __init__(self, resource, action):
        self.resource = resource
        self.action = action


class _Grant:
    def __init__(self, perm):
        self.permission = perm


class _RolePerm:
    def __init__(self, perm):
        self.permission = perm


class _Role:
    def __init__(self, name, perms=()):
        self.name = name
        self.permissions = [_RolePerm(p) for p in perms]


class _RoleLink:
    def __init__(self, role):
        self.role = role


class _User:
    def __init__(self, roles=(), grants=()):
        self.roles = [_RoleLink(r) for r in roles]
        self.permission_grants = [_Grant(p) for p in grants]


# ===========================================================================
# GAP 1 — Accounts → Users
# ===========================================================================


def test_permission_grid_matches_daves_layout():
    perms = staff_accounts.WORKPLACE_PERMISSIONS
    # The transcribed three-column grid enumerates 19 boxes.
    assert len(perms) == 19
    assert len({p.name for p in perms}) == 19
    assert all(1 <= p.column <= 3 for p in perms)
    assert all(1 <= p.row <= 7 for p in perms)
    # No two checkboxes occupy the same cell.
    assert len({(p.column, p.row) for p in perms}) == 19
    # The four conjunction-only entries are exactly the ones in the amber note.
    conjunction = {p.label for p in perms if p.conjunction_only}
    assert conjunction == {
        "Edit/Reverse repayment transaction",
        "Assignment officer",
        "Branch management",
        "Document verification",
    }
    for label in conjunction:
        assert label in staff_accounts.CONJUNCTION_NOTE


def test_permission_names_are_resource_dot_action():
    for p in staff_accounts.WORKPLACE_PERMISSIONS:
        assert p.name == f"{p.resource}.{p.action}"
        assert len(p.name) <= 100      # permissions.name is String(100)
        assert len(p.resource) <= 50   # permissions.resource is String(50)
        assert len(p.action) <= 50


def test_no_password_field_anywhere_in_staff_account_writes():
    """Dave's hard rule: an admin can never set a colleague's initial password."""
    import inspect

    source = inspect.getsource(staff_accounts)
    for fn in (
        staff_accounts.create_user,
        staff_accounts.update_user,
        staff_accounts.resend_invite,
    ):
        params = inspect.signature(fn).parameters
        assert not any("password" in name for name in params), fn.__name__
    # The only place a password hash is written is invite ACCEPTANCE, where the
    # invitee supplies it themselves.
    assert source.count("get_password_hash(") == 1
    assert "get_password_hash(new_password)" in source


def test_direct_grants_are_additive_and_never_subtractive():
    perm = _Perm("hardship", "manage")
    # A user with the role grant keeps it whether or not they hold a direct one.
    role_user = _User(roles=[_Role("staff", [perm])])
    assert user_has_permission(role_user, "hardship", "manage") is True
    assert user_has_direct_grant(role_user, "hardship", "manage") is False

    # A direct grant alone is enough.
    direct_user = _User(roles=[_Role("staff")], grants=[perm])
    assert user_has_permission(direct_user, "hardship", "manage") is True

    # And a user with neither is still denied — the pre-existing behaviour.
    plain = _User(roles=[_Role("staff")])
    assert user_has_permission(plain, "hardship", "manage") is False


def test_direct_grant_lookup_tolerates_objects_without_the_relationship():
    """Synthetic users in older tests have no `permission_grants` attribute;
    the helper must not raise for them (behaviour preservation)."""

    class Bare:
        roles = []

    assert user_has_direct_grant(Bare(), "hardship", "manage") is False
    assert user_has_permission(Bare(), "hardship", "manage") is False


def test_invite_state_transitions():
    from datetime import datetime, timedelta

    class U:
        password_hash = None
        password_reset_token = None
        password_reset_expires = None

    u = U()
    assert staff_accounts.invite_state(u) == "invite_expired"

    u.password_reset_token = "tok"
    u.password_reset_expires = datetime.utcnow() + timedelta(days=3)
    assert staff_accounts.invite_state(u) == "invited"

    u.password_reset_expires = datetime.utcnow() - timedelta(days=1)
    assert staff_accounts.invite_state(u) == "invite_expired"

    u.password_hash = "$2b$..."
    assert staff_accounts.invite_state(u) == "active"


def test_invite_ttl_is_finite_and_longer_than_a_password_reset():
    from datetime import timedelta

    assert timedelta(hours=1) < staff_accounts.INVITE_TTL <= timedelta(days=14)


# ===========================================================================
# GAP 2 — Product config depth
# ===========================================================================


def test_defaults_encode_current_engine_behaviour():
    cfg = ProductPolicyConfig()
    assert cfg.visible_to_customer is True
    sb = cfg.schedule_building
    assert sb.loan_type is LoanType.DAILY_SIMPLE_INTEREST
    assert sb.late_grace_days == 0
    assert sb.calculation_basis is CalculationBasis.REMAINING_PRINCIPAL
    assert sb.loan_phases == [LoanPhase.MAIN_PERIOD]
    assert sb.enable_customizable_equal_payments is False
    # The two default orders DIFFER — that is Dave's spec, verbatim.
    assert tuple(cfg.allocation_priority.accrual) == DEFAULT_ACCRUAL_PRIORITY
    assert tuple(cfg.allocation_priority.repayment) == DEFAULT_REPAYMENT_PRIORITY
    assert DEFAULT_ACCRUAL_PRIORITY != DEFAULT_REPAYMENT_PRIORITY


def test_empty_policy_config_still_parses_to_defaults():
    """Every existing product row has policy_config NULL — unchanged."""
    for raw in (None, {}):
        cfg = parse_product_policy_config(raw)
        assert cfg == ProductPolicyConfig()


def test_default_repayment_order_matches_the_allocation_engine():
    """interest → principal → scheduled fees → nsf(add-on), i.e. exactly what
    interest_engine.allocate_regular_payment does today."""
    from app.services.interest_engine import allocate_regular_payment, BalanceView

    order = list(DEFAULT_REPAYMENT_PRIORITY)
    assert order.index("interest") < order.index("principal")
    assert order.index("principal") < order.index("origination")
    assert order.index("principal") < order.index("administration")
    assert order[-1] == "nsf"

    # And prove it against the engine: a payment smaller than the total debt
    # covers interest first and never touches the add-on (NSF) bucket.
    import datetime as _dt

    balances = BalanceView(
        as_of=_dt.date(2026, 7, 22),
        outstanding_principal_cents=100_000,
        interest_due_cents=5_000,
        fees_due_cents=2_500,
        add_on_balance_cents=4_500,
    )
    alloc = allocate_regular_payment(6_000, balances)
    assert alloc.interest_cents == 5_000     # interest first
    assert alloc.principal_cents == 1_000    # then principal
    assert alloc.fees_cents == 0             # fees only after principal
    assert alloc.add_on_cents == 0           # NSF untouched — hence last


def test_orderings_must_be_a_full_permutation():
    for field in ("accrual", "repayment"):
        with pytest.raises(ProductPolicyConfigError):
            parse_product_policy_config({"allocation_priority": {field: ["interest"]}})
        with pytest.raises(ProductPolicyConfigError):
            parse_product_policy_config(
                {"allocation_priority": {field: list(ALLOCATION_CATEGORIES) + ["bogus"]}}
            )


@pytest.mark.parametrize(
    "bad_order",
    [
        ["principal", "interest", "origination", "administration", "nsf"],  # principal first
        ["interest", "origination", "principal", "administration", "nsf"],  # fee before principal
        ["interest", "principal", "nsf", "origination", "administration"],  # nsf not last
    ],
)
def test_repayment_order_the_engine_cannot_honour_is_refused(bad_order):
    with pytest.raises(ProductPolicyConfigError):
        parse_product_policy_config({"allocation_priority": {"repayment": bad_order}})


def test_swapping_the_two_shared_bucket_fees_is_allowed():
    """origination and administration share one engine bucket, so their relative
    order is a no-op the engine can honour."""
    cfg = parse_product_policy_config(
        {
            "allocation_priority": {
                "repayment": ["interest", "principal", "administration", "origination", "nsf"]
            }
        }
    )
    assert cfg.allocation_priority.repayment[2] == "administration"


def test_accrual_order_is_descriptive_and_unconstrained_by_the_engine():
    cfg = parse_product_policy_config(
        {
            "allocation_priority": {
                "accrual": ["nsf", "principal", "interest", "administration", "origination"]
            }
        }
    )
    assert cfg.allocation_priority.accrual[0] == "nsf"


def test_annuity_loan_type_is_declared_but_not_selectable():
    assert LoanType.ANNUITY.value == "annuity"
    with pytest.raises(ProductPolicyConfigError, match="daily simple interest"):
        parse_product_policy_config({"schedule_building": {"loan_type": "annuity"}})


def test_late_grace_days_bounds():
    assert parse_product_policy_config(
        {"schedule_building": {"late_grace_days": 5}}
    ).schedule_building.late_grace_days == 5
    with pytest.raises(ProductPolicyConfigError):
        parse_product_policy_config({"schedule_building": {"late_grace_days": -1}})


def test_loan_phases_must_not_be_empty():
    with pytest.raises(ProductPolicyConfigError):
        parse_product_policy_config({"schedule_building": {"loan_phases": []}})
    cfg = parse_product_policy_config(
        {"schedule_building": {"loan_phases": ["grace_period", "main_period", "main_period"]}}
    )
    assert cfg.schedule_building.loan_phases == [LoanPhase.GRACE_PERIOD, LoanPhase.MAIN_PERIOD]


def test_loan_type_destructive_change_guard():
    incoming = ProductPolicyConfig()

    # No stored config (the state of every product today) — nothing to guard.
    assert_loan_type_change_allowed(None, incoming)
    assert_loan_type_change_allowed({}, incoming)

    # Same loan type — no confirmation needed.
    assert_loan_type_change_allowed(
        {"schedule_building": {"loan_type": "daily_simple_interest"}}, incoming
    )

    # Different loan type — refused without the explicit confirm...
    stored = {"schedule_building": {"loan_type": "annuity"}}
    with pytest.raises(LoanTypeChangeError):
        assert_loan_type_change_allowed(stored, incoming)
    # ...and allowed with it (the API equivalent of clicking Confirm).
    assert_loan_type_change_allowed(stored, incoming, confirm_loan_type_change=True)


def test_visible_to_customer_round_trips():
    cfg = parse_product_policy_config({"visible_to_customer": False})
    assert cfg.visible_to_customer is False
    assert cfg.model_dump(mode="json")["visible_to_customer"] is False


def test_product_provinces_column_already_exists():
    """Gap 2 asked us to verify before adding — it was added by the compliance
    work and is already writable through create/PATCH."""
    from app.api.schemas.credit_products import CreditProductUpdate
    from app.models.platform.credit_product import PlatformCreditProduct

    assert "provinces" in PlatformCreditProduct.__table__.columns
    assert "provinces" in CreditProductUpdate.model_fields


# ===========================================================================
# GAP 3 — Integrations depth
# ===========================================================================


def test_flinks_defaults_are_daves_screen_values():
    cfg = FlinksConfig()
    assert cfg.when_to_verify is BankVerificationTiming.END_OF_APPLICATION
    assert cfg.verification_expiry_days == 10
    assert cfg.verification_reminder_days == 3
    assert cfg.transaction_depth_days == 90
    assert cfg.allow_customer_skip is True
    assert cfg.score_provided_data is True
    assert cfg.test_mode is True
    assert cfg.currency == "CAD"


def test_equifax_quad_minus_the_credential():
    cfg = EquifaxConfig()
    fields = set(EquifaxConfig.model_fields)
    assert {"member_number", "customer_code", "environment"} <= fields
    # The security code is a CREDENTIAL — it must not be a readable config field.
    assert "security_code" not in fields
    assert "security_code" in SECRET_KEYS["equifax"]
    assert cfg.environment is BureauEnvironment.TEST


def test_no_typed_config_field_looks_like_a_secret():
    banned = ("secret", "password", "api_key", "token", "security_code")
    for provider, model in PROVIDER_CONFIG_SCHEMAS.items():
        for name in model.model_fields:
            assert not any(b in name for b in banned), f"{provider}.{name}"


def test_unknown_providers_pass_through_untouched():
    raw = {"anything": "goes", "nested": {"a": 1}}
    assert validate_provider_config("zumrails", raw) == raw
    assert resolve_provider_config("zumrails", raw) == raw


def test_legacy_rows_survive_a_read_modify_write_round_trip():
    """Pre-schema rows carry arbitrary keys; typing must not drop them."""
    legacy = {"customer_id": "1af83d9d", "some_legacy_key": "keep me"}
    resolved = resolve_provider_config("flinks", legacy)
    assert resolved["some_legacy_key"] == "keep me"
    assert resolved["customer_id"] == "1af83d9d"
    assert resolved["verification_expiry_days"] == 10  # default materialised
    assert validate_provider_config("flinks", resolved)["some_legacy_key"] == "keep me"


def test_reads_are_tolerant_but_writes_validate():
    broken = {"verification_expiry_days": "not a number"}
    # Read: returned as-is rather than 500-ing an admin page.
    assert resolve_provider_config("flinks", broken) == broken
    # Write: refused.
    with pytest.raises(IntegrationConfigError):
        validate_provider_config("flinks", broken)


def test_expiry_and_reminder_ranges():
    with pytest.raises(IntegrationConfigError):
        validate_provider_config("flinks", {"verification_expiry_days": 0})
    with pytest.raises(IntegrationConfigError):
        validate_provider_config("flinks", {"transaction_depth_days": 10_000})


def test_redact_never_exposes_secret_values():
    """The write-only secrets invariant is unchanged by the typed config."""
    from app.services.integration_settings import redact

    class Row:
        provider = "equifax"
        config = {"member_number": "1234"}
        secrets = {"security_code": "SUPERSECRET", "client_secret": "shh"}
        enabled = True
        updated_by = None
        created_at = None
        updated_at = None

    out = redact(Row())
    assert out["secret_keys"] == ["client_secret", "security_code"]
    assert "SUPERSECRET" not in repr(out)
    assert "secrets" not in out
    assert out["config"]["member_number"] == "1234"
    assert out["config"]["environment"] == "test"  # default resolved
    assert out["expected_secret_keys"] == list(SECRET_KEYS["equifax"])
