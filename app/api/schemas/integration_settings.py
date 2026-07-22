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

    class Config:
        from_attributes = True


class IntegrationSettingsRead(BaseModel):
    """Redacted view of a provider's settings.

    Crucially this schema has NO `secrets` field — only `secret_keys`, the list
    of secret key NAMES that are set. Raw secret values never leave the service.
    """

    provider: str
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
