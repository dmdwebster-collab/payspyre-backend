from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field


# Known providers the admin area supports. `provider` is validated as a
# non-empty slug rather than a hard enum so new integrations can be added
# without a code change, but these are the expected values.
KNOWN_PROVIDERS = (
    "didit",
    "flinks",
    "sendgrid",
    "twilio",
    "zumrails",
    "signnow",
    "equifax",
    "google_analytics",
)


class IntegrationSettingsUpsert(BaseModel):
    """Request body for PUT /integration-settings/{provider}.

    `secrets` carries raw credential VALUES on write only — they are never
    echoed back in any response (see IntegrationSettingsRead).
    """

    config: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = False
    # SIMULATOR / LIVE. Omitted -> keep the existing row's mode (else simulator).
    # Switching to "live" requires the provider's credentials to be present or
    # the write is rejected 400.
    mode: Optional[str] = Field(
        default=None,
        description="Integration mode: 'simulator' or 'live'. Omit to keep the current mode.",
    )

    class Config:
        from_attributes = True


class IntegrationModeUpdate(BaseModel):
    """Request body for PUT /integration-settings/{provider}/mode.

    The focused Simulator/Live toggle — flips the mode without touching config or
    secrets. Switching to 'live' requires stored credentials or returns 400.
    """

    mode: str = Field(description="'simulator' or 'live'.")


class IntegrationSettingsRead(BaseModel):
    """Redacted view of a provider's settings.

    Crucially this schema has NO `secrets` field — only `secret_keys`, the list
    of secret key NAMES that are set. Raw secret values never leave the service.
    """

    provider: str
    # SIMULATOR / LIVE — the first-class integration mode the UI toggle binds to.
    mode: str = "simulator"
    # Whether the Live position is selectable yet (all required creds present).
    can_enable_live: bool = True
    # Which required secret keys are still missing for Live (labels the "why not"
    # when can_enable_live is false). Empty when live is available.
    missing_live_credentials: list[str] = Field(default_factory=list)
    # Readable BEHAVIOUR config. For flinks/equifax this is the typed shape from
    # app.schemas.integration_config with defaults resolved, so the admin UI
    # renders every knob even on a row saved before the schema landed.
    config: dict[str, Any]
    # Per-field editability contract: {field: {informational, consumed_by,
    # reason}}. Fields with informational=true have no consumer BY DESIGN and
    # MUST be rendered read-only — an editable-but-ignored control is a bug.
    config_meta: dict[str, Any] = Field(default_factory=dict)
    # Names of the secret keys that are populated (values redacted).
    secret_keys: list[str] = Field(default_factory=list)
    # Which secret keys this provider expects — labels the write-only inputs.
    expected_secret_keys: list[str] = Field(default_factory=list)
    enabled: bool
    updated_by: Optional[UUID] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True
