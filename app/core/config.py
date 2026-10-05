from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    PROJECT_NAME: str = "QueryMind"
    API_V1_STR: str = "/api/v1"
    SECRET_KEY: str = "changethis"

    # "production" is the safe default: it enables the outbound-host guard and
    # fails fast on missing secrets. Set ENVIRONMENT=development locally.
    ENVIRONMENT: Literal["development", "production"] = "production"

    # CORS
    CORS_ORIGINS: list[str] = ["http://localhost:3000"]

    # Database (Neon) — also stores schema vectors (pgvector)
    DATABASE_URL: str = ""

    # Google AI
    GOOGLE_API_KEY: str = ""

    # Security — Fernet key (generate: Fernet.generate_key().decode())
    ENCRYPTION_KEY: str = ""

    # Allow connections to private / loopback database hosts. Always allowed in
    # development; in production only when explicitly enabled (self-hosting).
    ALLOW_PRIVATE_DB_HOSTS: bool = False

    # Clerk Auth
    CLERK_ISSUER: str = ""
    CLERK_JWKS_URL: str = ""

    # Sentry (error monitoring) — empty disables it
    SENTRY_DSN: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore",
    )

    @property
    def private_db_hosts_allowed(self) -> bool:
        return self.ENVIRONMENT == "development" or self.ALLOW_PRIVATE_DB_HOSTS

    def missing_required(self) -> list[str]:
        """Names of required settings that are empty (checked at startup in production)."""
        required = ("DATABASE_URL", "GOOGLE_API_KEY", "ENCRYPTION_KEY", "CLERK_ISSUER", "CLERK_JWKS_URL")
        return [name for name in required if not getattr(self, name)]


settings = Settings()
