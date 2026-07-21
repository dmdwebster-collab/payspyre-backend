"""Typed application-process configuration (Wave 2 W2-APPCONFIG; TL videos 07-08
"Application process" — Application form, Application flow, Dictionaries,
Disclaimer, Co-applicant).

This is the platform-wide config for how the *applicant journey* is presented:
which form fields/sections show, the flow confirmations (origination
confirmation, loan-offer confirmation + 30-day expiry, multi-offer + max 3),
the editable dropdown dictionaries (employment / income types, rejection reasons
…), the submission disclaimer, and co-applicant behaviour.

Design rules (mirrors ``app/schemas/pricing_config.py`` + the decision-rules
registry pattern):

  * **Defaults preserve current behaviour.** An empty config row (the shipped
    default) reproduces exactly what the applicant flow does today: no custom
    fields, offer expiry 30 days, max 3 offers, co-applicant enabled but not
    required. Readers fall back to :data:`DEFAULT_APPLICATION_PROCESS_CONFIG`.
  * **This schema is descriptive, not executable.** Nothing here changes the
    submission code path; it is metadata the applicant flow + admin UI read.
    The one behaviourally-wired knob is offer-expiry/max-offers, and its
    defaults equal the current ``settings.OFFER_*`` values so the offer engine
    is unchanged until an admin edits them (see
    ``app/services/application_process_config.effective_offer_policy``).
  * **Dictionaries are display feeders, not the DB enum.** The
    ``platform_income_type`` / employment enums remain authoritative for stored
    data; these lists drive which choices a UI offers. Per Dave (parity mandate
    #5) the income-type dictionary ships WITHOUT Employment Insurance / Student.
  * Integer/string/bool primitives only — JSONB-round-trippable, pydantic-only
    (no app imports) so the service + API + tests can import it without cycles.
"""
from __future__ import annotations

import logging
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)

APP_PROCESS_SCHEMA_VERSION = 1

# Offer-flow defaults — MUST equal app.core.config.settings.OFFER_* so the
# offer engine's behaviour is unchanged when the config is at its default.
DEFAULT_OFFER_EXPIRY_DAYS = 30
DEFAULT_MAX_OFFERS = 3


class ApplicationProcessConfigError(ValueError):
    """An application_process config payload failed schema validation."""


# ---------------------------------------------------------------------------
# Application flow (TL 08 §2.8)
# ---------------------------------------------------------------------------

class ApplicationFlowConfig(BaseModel):
    """Flow confirmations + offer policy.

    ``require_origination_confirmation`` mirrors TL's toggle (borrower-created
    apps land in Origination vs auto-flow to Underwriting). It is stored for the
    flow/UI to read; the current platform behaviour is preserved regardless.
    """

    model_config = ConfigDict(extra="forbid")

    require_origination_confirmation: bool = True
    use_offer_confirmation: bool = True
    offer_expiry_days: int = Field(default=DEFAULT_OFFER_EXPIRY_DAYS, ge=1, le=365)
    allow_multiple_offers: bool = True
    max_offers: int = Field(default=DEFAULT_MAX_OFFERS, ge=1, le=25)


# ---------------------------------------------------------------------------
# Application form editor (TL 08 §2.7). Named variants (default/short/long) are
# assignable per credit product via ProductPolicyConfig.application_form_variant.
# ---------------------------------------------------------------------------

class FormFieldConfig(BaseModel):
    """One configurable field/section shown in the applicant form."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(..., min_length=1, max_length=100)
    label: str = Field(..., min_length=1, max_length=200)
    enabled: bool = True
    required: bool = False
    description: Optional[str] = Field(default=None, max_length=1000)


class FormVariantConfig(BaseModel):
    """A named form variant (e.g. ``default``/``short``/``long``).

    ``fields`` overlays/toggles built-in sections and adds custom fields. An
    empty ``fields`` list = the platform's built-in form untouched (current
    behaviour)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=100)
    label: str = Field(..., min_length=1, max_length=200)
    fields: list[FormFieldConfig] = Field(default_factory=list)

    @field_validator("fields")
    @classmethod
    def _unique_field_keys(cls, v: list[FormFieldConfig]) -> list[FormFieldConfig]:
        seen: set[str] = set()
        for f in v:
            if f.key in seen:
                raise ValueError(f"duplicate form field key {f.key!r}")
            seen.add(f.key)
        return v


# ---------------------------------------------------------------------------
# Dictionaries (TL 08 §2.9) — editable dropdown lists.
# ---------------------------------------------------------------------------

class DictionaryItem(BaseModel):
    """One lookup value: human title + unique code + optional description."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(..., min_length=1, max_length=64)
    title: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=1000)


# ---------------------------------------------------------------------------
# Disclaimer (TL 08 §2.10)
# ---------------------------------------------------------------------------

class DisclaimerConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(..., min_length=1, max_length=100)
    label: str = Field(..., min_length=1, max_length=500)


class DisclaimerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    require_confirmation: bool = True
    text: str = Field(default="Please check the box below to certify.", max_length=20_000)
    confirmations: list[DisclaimerConfirmation] = Field(
        default_factory=lambda: [DisclaimerConfirmation(key="submit", label="Submit")]
    )
    # Names of System-Document templates surfaced with the disclaimer.
    linked_documents: list[str] = Field(
        default_factory=lambda: ["terms_and_conditions", "privacy_policy"]
    )


# ---------------------------------------------------------------------------
# Co-applicant (TL 08 §2.13)
# ---------------------------------------------------------------------------

class CoApplicantConfig(BaseModel):
    """Echoes the TL audit-trail payload shape (IsEnabled/Label/…)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    label: str = Field(default="Co-Borrower", min_length=1, max_length=100)
    credit_bureau_check: bool = False
    required: bool = False
    cc_notifications: bool = True


# ---------------------------------------------------------------------------
# Default dictionaries (Dave's mandates baked in).
# ---------------------------------------------------------------------------

def _default_dictionaries() -> dict[str, list[DictionaryItem]]:
    return {
        # Employment types: FT / PT / seasonal (parity mandate #5).
        "employment_types": [
            DictionaryItem(code="full_time", title="Full-time"),
            DictionaryItem(code="part_time", title="Part-time"),
            DictionaryItem(code="seasonal", title="Seasonal"),
        ],
        # Income types WITHOUT Employment Insurance / Student (mandate #5). These
        # are display choices; the platform_income_type DB enum is unchanged.
        "income_types": [
            DictionaryItem(code="employed_full_time", title="Employed (full-time)"),
            DictionaryItem(code="employed_part_time", title="Employed (part-time)"),
            DictionaryItem(code="employed_seasonal", title="Employed (seasonal)"),
            DictionaryItem(code="self_employed", title="Self-employed"),
            DictionaryItem(code="retirement_pension", title="Retirement / pension"),
            DictionaryItem(code="disability", title="Disability"),
            DictionaryItem(code="other", title="Other"),
        ],
        "loan_rejection_reasons": [
            DictionaryItem(code="A01", title="Credit Score Below Minimum Requirements"),
            DictionaryItem(code="A03", title="Ability to Pay Below Minimum Requirements"),
            DictionaryItem(code="A05", title="Bankruptcy / Insolvency"),
            DictionaryItem(code="A07", title="Non-Resident"),
            DictionaryItem(code="A09", title="Application Error"),
        ],
        "loan_cancellation_reasons": [
            DictionaryItem(code="C01", title="Borrower Cancellation"),
            DictionaryItem(code="C03", title="Treatment Cancelled"),
            DictionaryItem(code="C05", title="Duplicate Application"),
        ],
        "loan_writeoff_reasons": [
            DictionaryItem(code="W01", title="Uncollectable"),
            DictionaryItem(code="W03", title="Insolvency / Consumer Proposal"),
        ],
    }


# ---------------------------------------------------------------------------
# Root document
# ---------------------------------------------------------------------------

class ApplicationProcessConfig(BaseModel):
    """The typed shape of ``platform_application_process_config.config``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=APP_PROCESS_SCHEMA_VERSION)
    flow: ApplicationFlowConfig = Field(default_factory=ApplicationFlowConfig)
    form_variants: list[FormVariantConfig] = Field(
        default_factory=lambda: [FormVariantConfig(name="default", label="Default")]
    )
    dictionaries: dict[str, list[DictionaryItem]] = Field(default_factory=_default_dictionaries)
    disclaimer: DisclaimerConfig = Field(default_factory=DisclaimerConfig)
    co_applicant: CoApplicantConfig = Field(default_factory=CoApplicantConfig)

    @field_validator("schema_version")
    @classmethod
    def _version(cls, v: int) -> int:
        if v != APP_PROCESS_SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported application_process config schema_version {v} "
                f"(expected {APP_PROCESS_SCHEMA_VERSION})"
            )
        return v

    @field_validator("form_variants")
    @classmethod
    def _variants_have_default(cls, v: list[FormVariantConfig]) -> list[FormVariantConfig]:
        names = [fv.name for fv in v]
        if len(names) != len(set(names)):
            raise ValueError("form_variants must have unique names")
        if "default" not in names:
            raise ValueError("form_variants must include a 'default' variant")
        return v

    @field_validator("dictionaries")
    @classmethod
    def _dictionaries_unique_codes(
        cls, v: dict[str, list[DictionaryItem]]
    ) -> dict[str, list[DictionaryItem]]:
        for name, items in v.items():
            codes = [i.code for i in items]
            if len(codes) != len(set(codes)):
                raise ValueError(f"dictionary {name!r} has duplicate item codes")
        return v

    def variant(self, name: Optional[str]) -> FormVariantConfig:
        """Return the named form variant, falling back to ``default``."""
        target = name or "default"
        for fv in self.form_variants:
            if fv.name == target:
                return fv
        return next(fv for fv in self.form_variants if fv.name == "default")


#: The shipped default — reproduces current applicant-flow behaviour exactly.
DEFAULT_APPLICATION_PROCESS_CONFIG = ApplicationProcessConfig()


def parse_application_process_config(
    raw: Optional[dict], *, context: str = ""
) -> ApplicationProcessConfig:
    """Parse a stored/submitted config into the typed schema.

    ``None`` / ``{}`` → shipped defaults (current behaviour). Any other payload
    is strictly validated (unknown keys rejected). Raises
    :class:`ApplicationProcessConfigError` on invalid payloads.
    """
    if raw is None or raw == {}:
        return ApplicationProcessConfig()
    if not isinstance(raw, dict):
        raise ApplicationProcessConfigError(
            f"application_process config must be an object, got {type(raw).__name__}"
        )
    try:
        return ApplicationProcessConfig.model_validate(raw)
    except ValidationError as exc:
        raise ApplicationProcessConfigError(
            f"application_process config failed schema validation: {exc}"
        ) from exc
