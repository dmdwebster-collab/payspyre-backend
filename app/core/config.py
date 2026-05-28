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
    VERSION: str = "0.1.0"

    # Observability
    SENTRY_DSN: str = ""

    # Database
    DATABASE_URL: str = "sqlite:///./payspyre.db"

    # JWT
    JWT_SECRET_KEY: str = "dev_jwt_secret_change_me"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
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
    USE_REAL_ADAPTERS: bool = False    # flip True in prod once Didit + Flinks creds are set

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

    # CSRF
    CSRF_ENABLED: bool = False

    # Notifications
    RESEND_API_KEY: str = ""
    RESEND_FROM_EMAIL: str = "noreply@payspyre.com"

    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_FROM_NUMBER: str = ""

    NOTIFICATION_QUEUE_PROCESSING_INTERVAL: int = 60
    NOTIFICATION_MAX_RETRIES: int = 3
    NOTIFICATION_RETRY_DELAYS: str = "5,15,30"

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
