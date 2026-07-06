from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Insecure dev defaults. Production must override these (enforced by
# Settings._require_secrets_in_production). DIDIT reuses the pre-existing
# DIDIT_WEBHOOK_SECRET field (shared with the V1 KYC webhook path) and is not
# guarded here — its prod lifecycle is owned by the existing deploy config.
PATIENT_JWT_DEV_DEFAULT = "dev_patient_jwt_secret_change_me"
FLINKS_WEBHOOK_DEV_DEFAULT = "dev_flinks_webhook_secret_change_me"
EQUIFAX_WEBHOOK_DEV_DEFAULT = "dev_equifax_webhook_secret_change_me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )

    # Application
    ENVIRONMENT: str = "development"
    # Dev/test helper endpoints (surface the mock magic-link code, simulate
    # verification results, seed a clinic) are auto-on in development/test. To expose
    # them anywhere else (e.g. a mock-mode staging beta) this must be EXPLICITLY set
    # true — they are UNAUTHENTICATED (anyone can mint a session/clinic JWT or
    # force-pass a verification), so never enable on an environment with real PII.
    # Production can never mount them regardless of this flag.
    ENABLE_DEV_TOOLS: bool = False
    # Shared secret guarding the admin RBAC seeder (POST /api/v1/admin/dev/seed-admin).
    # That endpoint mints a FULL-ADMIN cockpit user, so unlike the staff/clinic seeder
    # it is inert unless this is set AND the caller presents it in X-Dev-Seed-Token —
    # so deploying it does not create an open admin-granting backdoor.
    DEV_SEED_TOKEN: str = ""
    VERSION: str = "0.1.0"

    # Observability
    SENTRY_DSN: str = ""

    # Database
    DATABASE_URL: str = "sqlite:///./payspyre.db"

    # JWT
    JWT_SECRET_KEY: str = "dev_jwt_secret_change_me"
    JWT_ALGORITHM: str = "HS256"
    # Access-token lifetime for APPLICANT / borrower sessions (short, for security —
    # these are consumer-facing and often on shared/public devices).
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    # Access-token lifetime for STAFF / admin (lender operations cockpit) sessions.
    # The cockpit is an all-day internal tool someone stays signed into, so a 30-min
    # timeout mid-workday is disruptive. Scoped to staff/admin ONLY — the applicant
    # short lifetime above is unchanged. Refresh is unaffected (its own DAYS setting),
    # so a shorter value here would still be transparently refreshed; this just avoids
    # the mid-task re-auth. ~10 hours = one workday. The "Lock screen" idle-timeout is
    # a separate frontend concern.
    JWT_STAFF_ACCESS_TOKEN_EXPIRE_MINUTES: int = 600
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Patient (applicant) magic-link JWT — P6.5.
    # Dev default mirrors JWT_SECRET_KEY so local/CI runs work without extra env;
    # the model validator below makes production fail loud if it isn't overridden.
    PATIENT_JWT_SECRET: str = PATIENT_JWT_DEV_DEFAULT

    # Vendor webhook HMAC secrets — P6.6. (DIDIT reuses the existing
    # DIDIT_WEBHOOK_SECRET field below.) Dev defaults; prod must override.
    FLINKS_WEBHOOK_SECRET: str = FLINKS_WEBHOOK_DEV_DEFAULT
    EQUIFAX_WEBHOOK_SECRET: str = EQUIFAX_WEBHOOK_DEV_DEFAULT

    # KYC Vendors
    DIDIT_API_KEY: str = ""
    DIDIT_WEBHOOK_SECRET: str = ""

    PERSONA_API_KEY: str = ""
    PERSONA_WEBHOOK_SECRET: str = ""

    # Credit Bureaus
    EQUIFAX_API_KEY: str = ""
    TRANSUNION_API_KEY: str = ""

    # Real vendor outbound APIs + feature flag — P7.2 (initiate path only).
    # DIDIT_API_KEY already exists above. USE_REAL_ADAPTERS defaults False, so the
    # mock adapters stay active until production deliberately flips it on.
    DIDIT_API_BASE_URL: str = "https://verification.didit.me"
    DIDIT_WORKFLOW_ID: str = ""        # UUID from the Didit console; required for the real path
    FLINKS_API_KEY: str = ""           # flinks-auth-key header
    FLINKS_API_BASE_URL: str = "https://toolbox-api.private.fin.ag"
    FLINKS_CUSTOMER_ID: str = ""       # GUID scoping Flinks calls; required for the real path
    # Connect iframe origin (separate from the JSON API origin) + the applicant-facing
    # redirect base. Defaults target Flinks' shared sandbox ("toolbox"); override both
    # to point at a dedicated/prod instance.
    FLINKS_IFRAME_BASE: str = "https://toolbox-iframe.private.fin.ag"
    FLINKS_REDIRECT_URL_BASE: str = "https://app.payspyre.com/flinks/callback"
    USE_REAL_ADAPTERS: bool = False    # flip True in prod once Didit + Flinks creds are set

    # Real notification adapters + feature flag — P7.4 (outbound only; inbound
    # status callbacks deferred to P7.4b). When False (the default), the
    # applicant API binds MockNotificationDispatcher and no SMS / email is sent.
    # The Twilio + Resend slots above already exist; this flag flips the
    # selector in app/api/applicant/v1/deps.py:get_notification_dispatcher.
    USE_REAL_NOTIFICATIONS: bool = False

    # Document storage (DigitalOcean Spaces, S3-compatible) for KYC document uploads
    # (manual-application fallback). Inert until all four are set — see
    # app/services/storage/document_storage.py::is_configured. ENDPOINT defaults to the
    # DO Spaces regional endpoint derived from SPACES_REGION when left blank.
    SPACES_BUCKET: str = ""
    SPACES_KEY: str = ""
    SPACES_SECRET: str = ""
    SPACES_REGION: str = ""           # e.g. "tor1"
    SPACES_ENDPOINT: str = ""         # optional override; else https://<region>.digitaloceanspaces.com

    # Shared secret for the embedded pre-qualification widget (server-to-server intake).
    # The /api/v1/widget/pre-qualification endpoint is INERT (403) until this is set;
    # callers must send a matching X-Widget-Key header.
    WIDGET_API_KEY: str = ""

    # Inbound notification webhooks — P7.4b. Twilio reuses TWILIO_AUTH_TOKEN
    # for StatusCallback signature validation (Twilio convention). Resend ships
    # a per-endpoint Svix secret in "whsec_<base64>" form.
    # WEBHOOK_PUBLIC_BASE_URL is an optional override used by the Twilio
    # endpoint to reconstruct the URL Twilio actually signed (defeats
    # reverse-proxy scheme/host mismatch). When empty the endpoint falls back
    # to request.url (after honoring X-Forwarded-Proto).
    RESEND_WEBHOOK_SECRET: str = ""
    WEBHOOK_PUBLIC_BASE_URL: str = ""
    # Pre-send suppression gate — when True (default), the real notification
    # dispatcher rejects sends to recipients already on the suppression list
    # before the vendor call. Disable only for tests that need to bypass it.
    USE_SUPPRESSION_CHECK: bool = True

    # Borrower-facing portal base URL — used by the notification processor (WS2)
    # to build agreement / payment / account links in transactional + dunning
    # emails. Override per-env; no trailing slash.
    BORROWER_PORTAL_BASE_URL: str = "https://app.payspyre.com"

    # Observability — P8.0 PostHog bridge. Default disabled; flip per-env via
    # OBSERVABILITY_ENABLED=true once POSTHOG_API_KEY is set. The allowlist is
    # a CSV of platform_events event_type values; everything else is dropped
    # at the fan-out hook (no log noise). Hard Rule #6 (no PII) is enforced
    # inside posthog_bridge._safe_properties — never widen the allowlist
    # without re-reading that helper.
    POSTHOG_API_KEY: str = ""
    POSTHOG_HOST: str = "https://us.i.posthog.com"
    # Google Analytics (GA4) — product analytics, distinct from PostHog (internal obs).
    # Server-side Measurement Protocol; no-op when unset. Frontend uses gtag separately.
    GA_MEASUREMENT_ID: str = ""
    GA_API_SECRET: str = ""

    # App-layer encryption key (Fernet) for platform_integration_settings.secrets.
    # Empty in dev = no-op pass-through (plaintext). Set in prod to enable
    # encryption-at-rest. Generate: python -c "from cryptography.fernet import
    # Fernet; print(Fernet.generate_key().decode())"
    SETTINGS_ENCRYPTION_KEY: str = ""

    # DEDICATED app-layer encryption key (Fernet) for the borrower SIN, stored in
    # platform_patients.sin_encrypted. SEPARATE from SETTINGS_ENCRYPTION_KEY by
    # design (Hard Rule #7) so the SIN key can be rotated/scoped independently and
    # a compromise of the integration-credentials key never exposes SINs.
    # Empty in dev = no-op pass-through (plaintext); MUST be set in production.
    # Generate: python -c "from cryptography.fernet import Fernet;
    # print(Fernet.generate_key().decode())"
    SIN_ENCRYPTION_KEY: str = ""

    OBSERVABILITY_ENABLED: bool = False
    OBSERVABILITY_POSTHOG_ALLOWLIST: str = (
        "verification_completed,"
        "webhook_rejected,"
        "webhook_orphaned,"
        "notification_sent,"
        "notification_status_updated,"
        "decision_made,"
        "magic_link_issued"
    )

    # Collections / delinquency policy — vendor-dashboard KPIs (spec §4).
    # Grace window in days: an overdue installment is "late" while
    # 1 <= days_past_due <= GRACE_DAYS, and "delinquent" (bureau-reportable,
    # bucketed 30/60/90/120) at days_past_due >= 30. Lives in settings so
    # collections policy can tune the grace window without a code change.
    GRACE_DAYS: int = 15

    # CORS
    CORS_ORIGINS: str = "http://localhost:3000,https://payspyre.com"

    # Rate Limiting
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_AUTH_REQUESTS: int = 5
    RATE_LIMIT_AUTH_WINDOW: int = 60
    RATE_LIMIT_READ_REQUESTS: int = 100
    RATE_LIMIT_READ_WINDOW: int = 60
    RATE_LIMIT_WRITE_REQUESTS: int = 30
    RATE_LIMIT_WRITE_WINDOW: int = 60
    RATE_LIMIT_WEBHOOK_REQUESTS: int = 1000
    RATE_LIMIT_WEBHOOK_WINDOW: int = 60

    # Prometheus /metrics scrape auth. When set, /metrics requires
    # `Authorization: Bearer <token>`. When unset, /metrics is open in
    # non-production (dev/staging convenience) and DENIED in production — so the
    # KPI surface is never unauthenticated on a production deployment.
    METRICS_AUTH_TOKEN: str | None = None

    # CSRF
    CSRF_ENABLED: bool = False

    # Notifications
    RESEND_API_KEY: str = ""
    RESEND_FROM_EMAIL: str = "noreply@payspyre.com"

    SENDGRID_API_KEY: str = ""
    SENDGRID_FROM_EMAIL: str = "noreply@payspyre.com"
    # "sendgrid" | "resend" — which provider the real dispatcher uses for email.
    # Business uses SendGrid (dedicated IP), so it's the default.
    EMAIL_PROVIDER: str = "sendgrid"

    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_FROM_NUMBER: str = ""

    NOTIFICATION_QUEUE_PROCESSING_INTERVAL: int = 60
    NOTIFICATION_MAX_RETRIES: int = 3
    NOTIFICATION_RETRY_DELAYS: str = "5,15,30"
    # Durable notification retry outbox (P7.4c). When True, a vendor send that
    # fails transiently is enqueued into ``platform_notification_outbox`` for an
    # out-of-band worker (scripts/process_notification_outbox.py) to retry,
    # honoring NOTIFICATION_MAX_RETRIES + the NOTIFICATION_RETRY_DELAYS backoff.
    # INERT by default: nothing is enqueued and no worker runs until flipped, so
    # existing send semantics (rollback on failure) are unchanged out of the box.
    NOTIFICATION_OUTBOX_ENABLED: bool = False

    # Webhooks
    WEBHOOK_TIMEOUT_SECONDS: int = 30
    WEBHOOK_MAX_RETRIES: int = 3

    # S3 Storage
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_S3_BUCKET: str = ""
    AWS_REGION: str = "us-east-1"

    # Stripe
    STRIPE_SECRET_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""

    @model_validator(mode="after")
    def _require_secrets_in_production(self):
        """Fail loud in production if any managed secret is still its dev default.

        Covers the secrets introduced with their own dev defaults (PATIENT_JWT —
        P6.5; FLINKS / EQUIFAX webhook — P6.6). DIDIT_WEBHOOK_SECRET is excluded:
        it predates P6.6 (default ""), is shared with the V1 KYC path, and its
        prod lifecycle is owned by the existing deploy config (Option A).
        """
        if self.ENVIRONMENT != "production":
            return self
        still_default = [
            name
            for name, value, dev_default in (
                ("PATIENT_JWT_SECRET", self.PATIENT_JWT_SECRET, PATIENT_JWT_DEV_DEFAULT),
                ("FLINKS_WEBHOOK_SECRET", self.FLINKS_WEBHOOK_SECRET, FLINKS_WEBHOOK_DEV_DEFAULT),
                ("EQUIFAX_WEBHOOK_SECRET", self.EQUIFAX_WEBHOOK_SECRET, EQUIFAX_WEBHOOK_DEV_DEFAULT),
                # Encryption-at-rest keys: empty = no-op pass-through (plaintext). In
                # production that means vendor secrets / SINs land unencrypted — fail loud.
                ("SETTINGS_ENCRYPTION_KEY", self.SETTINGS_ENCRYPTION_KEY, ""),
                ("SIN_ENCRYPTION_KEY", self.SIN_ENCRYPTION_KEY, ""),
            )
            if value == dev_default
        ]
        if still_default:
            raise ValueError(
                "These secrets must be set in production (still the insecure dev "
                f"default): {', '.join(still_default)}."
            )
        # P7.2: if real adapters are enabled, their outbound credentials must be set.
        if self.USE_REAL_ADAPTERS:
            missing = [
                name
                for name, value in (
                    ("DIDIT_API_KEY", self.DIDIT_API_KEY),
                    ("DIDIT_WORKFLOW_ID", self.DIDIT_WORKFLOW_ID),
                    ("FLINKS_API_KEY", self.FLINKS_API_KEY),
                    ("FLINKS_CUSTOMER_ID", self.FLINKS_CUSTOMER_ID),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    "USE_REAL_ADAPTERS is True but these required fields are empty: "
                    f"{', '.join(missing)}."
                )
        # P7.4: same shape — real notification outbound creds must be present.
        if self.USE_REAL_NOTIFICATIONS:
            email_required = (
                [("SENDGRID_API_KEY", self.SENDGRID_API_KEY),
                 ("SENDGRID_FROM_EMAIL", self.SENDGRID_FROM_EMAIL)]
                if self.EMAIL_PROVIDER == "sendgrid"
                else [("RESEND_API_KEY", self.RESEND_API_KEY),
                      ("RESEND_FROM_EMAIL", self.RESEND_FROM_EMAIL)]
            )
            missing_notif = [
                name
                for name, value in (
                    email_required
                    + [
                        ("TWILIO_ACCOUNT_SID", self.TWILIO_ACCOUNT_SID),
                        ("TWILIO_AUTH_TOKEN", self.TWILIO_AUTH_TOKEN),
                        ("TWILIO_FROM_NUMBER", self.TWILIO_FROM_NUMBER),
                    ]
                )
                if not value
            ]
            if missing_notif:
                raise ValueError(
                    "USE_REAL_NOTIFICATIONS is True but these required fields are empty: "
                    f"{', '.join(missing_notif)}."
                )
        return self

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, v):
        if isinstance(v, str):
            return v
        return ",".join(v) if isinstance(v, list) else v

    @property
    def cors_origins_list(self) -> list[str]:
        if isinstance(self.CORS_ORIGINS, str):
            return [origin.strip() for origin in self.CORS_ORIGINS.split(",")]
        return self.CORS_ORIGINS


settings = Settings()
