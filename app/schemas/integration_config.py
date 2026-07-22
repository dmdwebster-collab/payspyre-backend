"""Typed per-provider integration BEHAVIOUR config (Settings → Integrations).

We already store bare API keys per provider
(:mod:`app.services.integration_settings`: a free-form ``config`` JSONB plus an
encrypted, never-returned ``secrets`` JSONB). Dave's Integrations page carries
far more than keys — each provider block has behavioural knobs that drive when
and how the integration runs. This module gives the two providers that matter
most a typed, defaulted, validated shape for that ``config`` half.

    Flinks  (Bank account verification) — when to verify, verification expiry
            (10 d), verification reminder (3 d), transaction depth (90 d),
            allow-skip, test mode, logging, iframe/service URLs, currency.
    Equifax (Credit bureau)             — member number, customer code,
            environment, automatic request, request/response logging.

SECRETS BOUNDARY — unchanged. Anything credential-like stays in ``secrets``:
write-only, envelope-encrypted, never echoed (API output exposes only which
KEYS are set). That includes Equifax's **security code** and client id/secret,
and Flinks' API key. :data:`SECRET_KEYS` documents the expected key names per
provider so the admin UI can label the write-only inputs. Everything modelled
below is readable behaviour config, not a credential.

BACK-COMPAT — every model is ``extra="allow"``. Rows written before this schema
existed carry arbitrary keys; validating them must never drop data or start
rejecting a previously-saved integration. Unknown keys survive a
read/modify/write round trip untouched, and missing behaviour fields resolve to
Dave's on-screen defaults.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class IntegrationConfigError(ValueError):
    """A provider's `config` payload failed typed validation."""


# ---------------------------------------------------------------------------
# Flinks — Bank account verification (rewatch_2026-07-21/07-08_settings.md §A6)
# ---------------------------------------------------------------------------


class BankVerificationTiming(str, Enum):
    """Dave's "When to conduct bank account verification" dropdown."""

    #: The value shown on his screen (truncated on-screen as
    #: "Bank verification at the end of the loan applicatio…").
    END_OF_APPLICATION = "end_of_application"
    START_OF_APPLICATION = "start_of_application"
    AFTER_APPROVAL = "after_approval"
    MANUAL_ONLY = "manual_only"


class FlinksConfig(BaseModel):
    """Bank-account-verification behaviour. Defaults are Dave's screen values.

    ``verification_expiry_days`` / ``verification_reminder_days`` are the pair
    the ``bank_account_verification_reminder`` / ``_expired`` notification types
    already reference but had no configurable schedule for.
    """

    model_config = ConfigDict(extra="allow")

    when_to_verify: BankVerificationTiming = BankVerificationTiming.END_OF_APPLICATION
    score_provided_data: bool = True
    verification_expiry_days: int = Field(default=10, ge=1, le=365)
    verification_reminder_days: int = Field(default=3, ge=1, le=365)
    allow_customer_skip: bool = True
    transaction_depth_days: int = Field(
        default=90, ge=1, le=730, description='Dave: "Depth of account report = 90 days"'
    )
    test_mode: bool = True
    use_attributes: bool = True
    log_response: bool = True
    currency: str = Field(default="CAD", min_length=3, max_length=3)
    customer_id: Optional[str] = Field(default=None, max_length=200)
    service_url: Optional[str] = Field(default=None, max_length=500)
    iframe_url: Optional[str] = Field(default=None, max_length=500)


# ---------------------------------------------------------------------------
# Equifax — Credit bureau (§A6)
# ---------------------------------------------------------------------------


class BureauEnvironment(str, Enum):
    TEST = "test"
    PRODUCTION = "production"


class EquifaxConfig(BaseModel):
    """Credit-bureau behaviour + the non-secret half of Dave's Equifax quad.

    The quad is member number · security code · customer code · environment.
    Three of the four are identifiers and live here, readable. The **security
    code is a credential** and therefore lives in ``secrets`` alongside client
    id / client secret — write-only, never returned. Splitting it that way is
    the only deviation from Dave's screen and it is deliberate.
    """

    model_config = ConfigDict(extra="allow")

    member_number: Optional[str] = Field(default=None, max_length=100)
    customer_code: Optional[str] = Field(default=None, max_length=100)
    environment: BureauEnvironment = BureauEnvironment.TEST
    automatic_request: bool = False
    log_request: bool = False
    log_response: bool = False


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

#: Providers with a typed behaviour schema. Any other provider keeps the
#: existing free-form `config` dict, unvalidated — exactly as before.
PROVIDER_CONFIG_SCHEMAS: dict[str, type[BaseModel]] = {
    "flinks": FlinksConfig,
    "equifax": EquifaxConfig,
}

#: Which `secrets` keys each provider expects. Documentation for the admin UI's
#: write-only inputs — NOT a validation rule (secrets are never inspected here).
SECRET_KEYS: dict[str, tuple[str, ...]] = {
    "flinks": ("api_key",),
    "equifax": ("security_code", "client_id", "client_secret"),
}


def validate_provider_config(provider: str, config: Optional[dict]) -> dict:
    """Validate + default a provider's `config` on write.

    Unknown providers pass through unchanged (no behaviour change). Known
    providers are validated and returned with defaults materialised, so the
    stored row is self-describing and a GET always shows every knob.
    """
    config = config or {}
    model = PROVIDER_CONFIG_SCHEMAS.get(provider)
    if model is None:
        return config
    try:
        parsed = model.model_validate(config)
    except ValidationError as exc:
        raise IntegrationConfigError(
            f"{provider} config failed validation: {exc}"
        ) from exc
    return parsed.model_dump(mode="json")


def resolve_provider_config(provider: str, config: Optional[dict]) -> dict[str, Any]:
    """Read-side resolution: stored config + defaults for anything absent.

    Tolerant by design — a legacy row with a value this schema would reject is
    returned AS-IS rather than 500-ing a read. Writes still validate.
    """
    config = config or {}
    model = PROVIDER_CONFIG_SCHEMAS.get(provider)
    if model is None:
        return config
    try:
        return model.model_validate(config).model_dump(mode="json")
    except ValidationError:
        return config
