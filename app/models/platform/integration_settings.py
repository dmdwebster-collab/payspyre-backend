from uuid import uuid4

from sqlalchemy import Column, DateTime, String, Boolean, func
from sqlalchemy.dialects.postgresql import UUID, JSONB

from app.db.base import Base


class PlatformIntegrationSettings(Base):
    """Per-provider integration configuration, managed by admins.

    One row per external integration (Didit, Flinks, SendGrid, Twilio,
    Zumrails, SignNow, Equifax, Google Analytics). Lets the team enter
    credentials/config through the admin area instead of the developer
    hardcoding them.

    SECURITY — ENCRYPTION-AT-REST GAP:
    `secrets` holds credential VALUES (api keys, tokens, client secrets) as
    plaintext JSONB for now. This mirrors the *intended* SIN approach
    (PlatformPatient.sin_encrypted, "pgcrypto encrypted") — secret material
    must be encrypted at rest. That encryption is NOT yet implemented here; it
    is a known gap to close before real production credentials are stored.
    The service layer never returns raw secret values in API output (it
    redacts to which keys are set), and secrets must never be logged or written
    to platform_events.
    """

    __tablename__ = "platform_integration_settings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)

    # Provider slug, e.g. 'sendgrid', 'flinks', 'zumrails'. Unique.
    provider = Column(String, nullable=False, unique=True)

    # Non-secret configuration — safe to return in API responses.
    config = Column(JSONB, nullable=False, default=dict)

    # SECRET credential values. ENCRYPTION-AT-REST GAP (see class docstring):
    # plaintext JSONB for now; never returned raw by the API.
    secrets = Column(JSONB, nullable=False, default=dict)

    enabled = Column(Boolean, nullable=False, default=False)

    # Auth user id of the admin who last wrote this row. Nullable for
    # system/seed writes.
    updated_by = Column(UUID(as_uuid=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<PlatformIntegrationSettings(provider={self.provider}, "
            f"enabled={self.enabled})>"
        )
