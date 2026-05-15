from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # KYC Vendors
    DIDIT_API_KEY: str = ""
    DIDIT_WEBHOOK_SECRET: str = ""

    PERSONA_API_KEY: str = ""
    PERSONA_WEBHOOK_SECRET: str = ""

    # Credit Bureaus
    EQUIFAX_API_KEY: str = ""
    TRANSUNION_API_KEY: str = ""

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
