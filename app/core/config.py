"""Application settings, loaded from environment variables / .env (see .env.example)."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "local"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://oscu:oscu@localhost:5432/oscu_gateway"

    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    kra_env: Literal["sandbox", "production"] = "sandbox"
    kra_base_url_prod: str = "https://etims-api.kra.go.ke/etims-api/"
    kra_base_url_sbx: str = "https://etims-api-sbx.kra.go.ke/etims-api/"
    kra_request_timeout_seconds: int = 30

    cmc_key_encryption_key: str = "changeme-generate-a-real-fernet-key"

    jwt_secret_key: str = "changeme-generate-a-real-secret"
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 30

    @property
    def kra_base_url(self) -> str:
        return self.kra_base_url_sbx if self.kra_env == "sandbox" else self.kra_base_url_prod


@lru_cache
def get_settings() -> Settings:
    return Settings()
