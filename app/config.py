from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    redis_url: str = "redis://localhost:6380/0"


@lru_cache
def get_settings() -> Settings:
    return Settings()
