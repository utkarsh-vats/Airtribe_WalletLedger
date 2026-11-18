from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from environment variables (and `.env` when present)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"

    # Host-side defaults match docker-compose port mappings; containers override these.
    database_url: str = "postgresql+asyncpg://walletledger:walletledger@localhost:5433/walletledger"
    redis_url: str = "redis://localhost:6380/0"


@lru_cache
def get_settings() -> Settings:
    return Settings()
