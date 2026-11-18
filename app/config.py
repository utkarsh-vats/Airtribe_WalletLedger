from functools import lru_cache
from typing import Literal, Self

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Local-development default only. Settings refuses it when APP_ENV=prod.
DEV_JWT_SECRET = "dev-only-insecure-jwt-secret-change-me-0123456789"  # noqa: S105
MIN_JWT_SECRET_BYTES = 32  # HS256 key should be at least as long as the hash output


class Settings(BaseSettings):
    """Runtime configuration, read from environment variables (and `.env` when present)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"

    # Host-side defaults match docker-compose port mappings and default passwords; containers
    # override these. The app connects as the least-privilege runtime role `wallet_app`.
    database_url: str = "postgresql+asyncpg://wallet_app:wallet_app@localhost:5433/walletledger"
    # Integration tests connect here only, never to database_url (enforced in tests/conftest.py).
    test_database_url: str = (
        "postgresql+asyncpg://wallet_app:wallet_app@localhost:5433/walletledger_test"
    )
    # Migrations run as the schema owner `wallet_owner` (enforced in migrations/env.py).
    migration_database_url: str = (
        "postgresql+asyncpg://wallet_owner:wallet_owner@localhost:5433/walletledger"
    )
    test_migration_database_url: str = (
        "postgresql+asyncpg://wallet_owner:wallet_owner@localhost:5433/walletledger_test"
    )
    redis_url: str = "redis://localhost:6380/0"

    # HS256 signing key for access tokens (DESIGN.md §6: 60-minute tokens, no refresh).
    jwt_secret: SecretStr = SecretStr(DEV_JWT_SECRET)
    jwt_ttl_minutes: int = 60

    @model_validator(mode="after")
    def _check_jwt_secret(self) -> Self:
        secret = self.jwt_secret.get_secret_value()
        if len(secret.encode()) < MIN_JWT_SECRET_BYTES:
            raise ValueError(f"JWT_SECRET must be at least {MIN_JWT_SECRET_BYTES} bytes")
        if self.app_env == "prod" and secret == DEV_JWT_SECRET:
            raise ValueError("JWT_SECRET must be set to a private value when APP_ENV=prod")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
