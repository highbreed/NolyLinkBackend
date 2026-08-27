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

    # First lastReqDt to send for a resource never synced before. Not specified anywhere in
    # the source spec — a config flag rather than a hardcoded guess (spec §11).
    kra_sync_initial_last_req_dt: str = "20200101000000"

    # spec §20 item 2 / §7: unconfirmed whether Nation/Packaging/Quantity/Currency come back
    # through /selectCodeList's normal cdCls mechanism, or are distributed separately. True
    # (the spec text's own default reading) until sandbox access confirms otherwise; flipping
    # it activates code_cache_service's static-seed fallback for those four classifications.
    kra_large_code_tables_included_in_code_list: bool = True

    # Polling cadence per resource (spec §11), in seconds.
    kra_poll_interval_codes_seconds: int = 6 * 60 * 60
    kra_poll_interval_notices_seconds: int = 60 * 60
    kra_poll_interval_import_items_seconds: int = 15 * 60
    kra_poll_interval_purchase_sales_seconds: int = 15 * 60
    kra_poll_interval_stock_moves_seconds: int = 15 * 60

    # How often outbox_dispatcher checks for due rows (spec §14). Independent of the backoff
    # schedule itself (30s/2m/10m/1h) — this just bounds how promptly a due retry is noticed.
    kra_outbox_dispatch_interval_seconds: int = 30

    @property
    def kra_base_url(self) -> str:
        return self.kra_base_url_sbx if self.kra_env == "sandbox" else self.kra_base_url_prod


@lru_cache
def get_settings() -> Settings:
    return Settings()
