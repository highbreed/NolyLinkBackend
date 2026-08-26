"""Application settings, loaded from environment variables / .env (see .env.example)."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "local"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://oscu:oscu@localhost:5432/oscu_gateway"
    # Tests run against this database, never against database_url, so a test run's
    # create_all/drop_all can't ever touch real dev/prod data. Defaults to database_url's
    # database name with a "_test" suffix if not set explicitly.
    test_database_url: str | None = None

    @property
    def resolved_test_database_url(self) -> str:
        if self.test_database_url:
            return self.test_database_url
        url = make_url(self.database_url)
        return url.set(database=f"{url.database}_test").render_as_string(hide_password=False)

    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    kra_env: Literal["sandbox", "production"] = "sandbox"
    kra_base_url_prod: str = "https://etims-api.kra.go.ke/etims-api/"
    kra_base_url_sbx: str = "https://etims-api-sbx.kra.go.ke/etims-api/"
    kra_request_timeout_seconds: int = 30
    # Spec §5.1: the source document doesn't print an explicit URL for item-composition save,
    # so the path is a config flag rather than a hardcoded assumption (confirm before go-live).
    kra_item_composition_endpoint: str = "/saveItemComposition"

    cmc_key_encryption_key: str = "changeme-generate-a-real-fernet-key"

    jwt_secret_key: str = "changeme-generate-a-real-secret"
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 30

    # Provisional gate for POST /v1/devices/initialize, which must never be reachable by
    # SAP/ERP clients (spec §12.2). Phase 9 replaces this with the full client authN/Z system;
    # unset (None) means the endpoint refuses every request rather than defaulting open.
    admin_api_key: str | None = None

    @property
    def kra_base_url(self) -> str:
        return self.kra_base_url_sbx if self.kra_env == "sandbox" else self.kra_base_url_prod


@lru_cache
def get_settings() -> Settings:
    return Settings()
