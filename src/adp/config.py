"""Typed settings read from environment / .env. Field names match env var names."""

from functools import lru_cache

from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from adp.errors import ConfigError


class Settings(BaseSettings):
    """Platform settings; secrets are SecretStr and never shown in repr."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    adp_s3_endpoint: str = "http://127.0.0.1:9000"
    minio_root_user: str = "minioadmin"
    minio_root_password: SecretStr = SecretStr("change-me-locally")

    postgres_user: str = "adp"
    postgres_password: SecretStr = SecretStr("change-me-locally")
    postgres_db: str = "adp"
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5433

    adp_llm_monthly_budget_eur: float = 5.0
    anthropic_api_key: SecretStr | None = None


@lru_cache
def get_settings() -> Settings:
    """Return cached settings; raise ConfigError if invalid."""
    try:
        return Settings()
    except ValidationError as exc:
        raise ConfigError(str(exc)) from exc
