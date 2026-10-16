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
    # VSCU devices use this exact same request/response contract — only the host differs
    # (Branch.device_type / kra_base_url_override, app/services/kra_adapter.py). Left blank by
    # default rather than guessing a URL; must be set (env or per-branch override) before any
    # VSCU branch is initialized.
    kra_base_url_vscu_prod: str = ""
    kra_base_url_vscu_sbx: str = ""
    # The receipt-verification URL printed/QR-coded on every receipt (app/services/
    # kra_receipt.py's build_qr_link) — a different host from kra_base_url_* above (that's the
    # API host the gateway calls; this is the public page a customer's phone scans). Split
    # sandbox/production the same way, since a sandbox-fiscalized receipt's QR must point at
    # the sandbox verification host or it won't resolve. Both confirmed working directly
    # (2026-10-16): sbx against a real sandbox-fiscalized invoice, prod via a user-supplied
    # real working example.
    kra_receipt_verification_base_url_prod: str = (
        "https://etims.kra.go.ke/common/link/etims/receipt/indexEtimsReceiptData"
    )
    kra_receipt_verification_base_url_sbx: str = (
        "https://etims-sbx.kra.go.ke/common/link/etims/receipt/indexEtimsReceiptData"
    )
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

    # Admin portal login (app/services/admin_user_auth.py) — sits alongside admin_api_key
    # above, not in place of it. Longer-lived than client tokens by default since this is a
    # human's browser session, not a machine-to-machine credential re-requested on every 401.
    admin_jwt_access_token_expire_minutes: int = 60

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
    kra_poll_interval_item_classes_seconds: int = 6 * 60 * 60
    kra_poll_interval_notices_seconds: int = 60 * 60
    kra_poll_interval_import_items_seconds: int = 15 * 60
    kra_poll_interval_purchase_sales_seconds: int = 15 * 60
    kra_poll_interval_stock_moves_seconds: int = 15 * 60

    # How often outbox_dispatcher checks for due rows (spec §14). Independent of the backoff
    # schedule itself (30s/2m/10m/1h) — this just bounds how promptly a due retry is noticed.
    kra_outbox_dispatch_interval_seconds: int = 30

    # Per-client rate limit (spec §15), a fixed-window counter in Redis. Not specified by the
    # spec — a config flag rather than a hardcoded guess.
    client_rate_limit_per_minute: int = 120

    # Billing/subscription enforcement (this gateway's own tenant billing — not part of the
    # KRA spec at all). Defaults OFF: flipping this on before every existing Client has a
    # Subscription row would lock all of them out (app/services/billing_service.py fails
    # closed — no subscription on file == blocked — once enabled), so this must be a
    # deliberate cutover, not implied by deploying the code.
    billing_enforcement_enabled: bool = False
    # Days a client keeps working past Subscription.current_period_end before being blocked —
    # absorbs invoice-processing lag so a late admin update doesn't look like an outage to a
    # paying customer. Overridable per-subscription (Subscription.grace_period_days).
    billing_grace_period_days: int = 7

    # --- VSCU integration layer (app/integrations/vscu/) ---
    # Superseded understanding as of 2026-09-01 (docs/kra_specs/README.md): VSCU is NOT "the
    # OSCU contract at KRA_BASE_URL_VSCU_*" — kra_base_url_vscu_prod/sbx above encode that
    # disproven assumption and are no longer used by any VSCU code path. A real VSCU device is
    # a separate local Spring Boot app (KRA's vscu-v2.0.21.jar) that this gateway calls over
    # the local network; that local app owns its own auth with real KRA (cmcKey/signKey/
    # intrlKey never leave it) and proxies upstream itself. So the only VSCU-relevant network
    # address we ever configure is vscu_base_url, pointed at that local app — never at
    # etims-api*.kra.go.ke.
    # Multi-tenant: each client picks OSCU or VSCU independently per branch
    # (Branch.device_type), and a VSCU device runs on that specific client's own premises —
    # there is no one "the VSCU host" the way kra_base_url is one host for every OSCU branch.
    # So vscu_base_url below is only a fallback default (single-tenant/dev, or before a
    # branch's own VscuDeviceState.base_url is set); the real per-branch address always lives
    # on VscuDeviceState (app/db/models.py), resolved the same way KraAdapter resolves
    # kra_base_url_override — see app/integrations/vscu/client.py.
    vscu_enabled: bool = False
    vscu_base_url: str = "http://127.0.0.1:8088"
    vscu_connect_timeout_seconds: float = 10.0
    vscu_read_timeout_seconds: float = 60.0
    # Informational only (which upstream KRA env a given branch's local VSCU app is pointed
    # at, per its own vscu.properties `profile` setting) — this gateway never uses it to build
    # a URL, and different branches/tenants may legitimately differ.
    vscu_environment: Literal["sandbox", "production"] = "sandbox"
    # dvcSrlNo for /initializer/selectInitInfo (2026-09-01, per direct operator confirmation):
    # unlike OSCU's dvcSrlNo (generate_dvc_srl_no below, a value *we* invent per device), VSCU's
    # is issued *to us* by KRA — one domain-wide serial number for this integration, not
    # per-branch and not per-customer. So it belongs here as a single configured value (split
    # sandbox/production like kra_base_url_vscu_*, since KRA very plausibly issues a distinct
    # one per environment), not as an admin-supplied field on every device-init request. Still
    # overridable per-call (VSCUInitializeRequest.dvc_srl_no) for a customer whose VSCU device
    # was already initialized under a different value before this became a global default.
    vscu_dvc_srl_no_prod: str = ""
    vscu_dvc_srl_no_sbx: str = ""

    def vscu_dvc_srl_no_for_env(self) -> str:
        value = self.vscu_dvc_srl_no_sbx if self.kra_env == "sandbox" else self.vscu_dvc_srl_no_prod
        if not value:
            raise ValueError(
                f"No dvcSrlNo configured for VSCU (vscu_dvc_srl_no_{self.kra_env} is unset) — "
                "set it, or pass an explicit dvc_srl_no for this device."
            )
        return value

    @property
    def kra_base_url(self) -> str:
        return self.kra_base_url_sbx if self.kra_env == "sandbox" else self.kra_base_url_prod

    @property
    def kra_receipt_verification_base_url(self) -> str:
        return (
            self.kra_receipt_verification_base_url_sbx
            if self.kra_env == "sandbox"
            else self.kra_receipt_verification_base_url_prod
        )

    def kra_base_url_for(self, device_type: str) -> str:
        """Global default host for a device_type, before any per-branch override. Raises if
        VSCU is requested but its URL was never configured, rather than silently calling an
        empty host.
        """
        if device_type == "VSCU":
            url = (
                self.kra_base_url_vscu_sbx
                if self.kra_env == "sandbox"
                else self.kra_base_url_vscu_prod
            )
            if not url:
                raise ValueError(
                    f"device_type=VSCU but kra_base_url_vscu_{self.kra_env} is not configured "
                    "— set it, or pass an explicit kra_base_url_override for this branch."
                )
            return url
        return self.kra_base_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
