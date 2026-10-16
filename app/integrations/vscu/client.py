"""VSCUClient: httpx.AsyncClient wrapper for the local VSCU app, mirroring
app/services/kra_adapter.py's shape (base-URL resolution, call logging, resultCd -> exception
mapping, metrics) but for a genuinely different downstream: a per-branch local device, not one
shared KRA host. See app/integrations/vscu/__init__.py for why this isn't KraAdapter with a
different base URL.

Scope boundary, same as KraAdapter: this module owns *transport* (base URL resolution per
branch, timeouts, resultCd -> exception mapping, call logging). It does not retry fiscal
submissions — VSCUUnavailableError/VSCUTransportError are marked retryable as a hint, but only
the reconciliation service (a later phase) decides whether re-submitting a specific invoice is
actually safe.
"""

import hashlib
import json
import time
import uuid
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.metrics import vscu_request_duration_seconds, vscu_requests_total
from app.db.models import VscuCallLog, VscuDeviceState
from app.integrations.vscu import constants
from app.integrations.vscu.exceptions import (
    VSCUConfigurationError,
    VSCUDuplicateSubmissionError,
    VSCUError,
    VSCUErrorContext,
    VSCUSequenceError,
    VSCUTransportError,
    VSCUUnavailableError,
    VSCUUnknownError,
    VSCUValidationError,
)
from app.integrations.vscu.schemas.branch import (
    BhfCustSaveReq,
    BhfCustSaveRes,
    BhfInsuranceSaveReq,
    BhfInsuranceSaveRes,
    BhfReq,
    BhfRes,
    BhfUserSaveReq,
    BhfUserSaveRes,
)
from app.integrations.vscu.schemas.codes import CodeReq, CodeRes
from app.integrations.vscu.schemas.customer import CustReq, CustRes
from app.integrations.vscu.schemas.envelope import VSCUResponseEnvelope
from app.integrations.vscu.schemas.import_item import (
    ImportItemReq,
    ImportItemRes,
    ImportItemUpdateReq,
    ImportItemUpdateRes,
)
from app.integrations.vscu.schemas.init import InitInfoReq, InitInfoRes
from app.integrations.vscu.schemas.item import (
    ItemCompositionSaveReq,
    ItemCompositionSaveRes,
    ItemReq,
    ItemRes,
    ItemSaveReq,
    ItemSaveRes,
)
from app.integrations.vscu.schemas.item_class import ItemClsReq, ItemClsRes
from app.integrations.vscu.schemas.notice import NoticeReq, NoticeRes
from app.integrations.vscu.schemas.purchase import (
    TrnsPurchaseSalesReq,
    TrnsPurchaseSalesRes,
    TrnsPurchaseSaveReq,
    TrnsPurchaseSaveRes,
)
from app.integrations.vscu.schemas.report import (
    ReportZCheckReq,
    ReportZCheckRes,
    ReportZSaveReq,
    ReportZSaveRes,
)
from app.integrations.vscu.schemas.sales import (
    InvoiceDetailsReq,
    InvoiceDetailsRes,
    InvoicesRes,
    TrnsSalesListReq,
    TrnsSalesSaveWrReq,
    TrnsSalesSaveWrRes,
)
from app.integrations.vscu.schemas.stock import (
    StockIoSaveReq,
    StockIoSaveRes,
    StockMasterSaveReq,
    StockMasterSaveRes,
    StockMoveReq,
    StockMoveRes,
)
from app.integrations.vscu.schemas.test import EchoTestReq, EchoTestRes

ResT = TypeVar("ResT", bound=VSCUResponseEnvelope[Any])


def raise_for_result(
    endpoint: str, result_cd: str, result_msg: str | None, result_dt: str | None
) -> None:
    """Raise the matching VSCUError subclass for a non-success resultCd. See
    app/integrations/vscu/constants.py for exactly which codes are verified against the
    decompiled jar vs. passed-through-from-real-KRA.
    """
    if result_cd == constants.SUCCESS:
        return

    ctx = VSCUErrorContext(
        endpoint=endpoint, result_cd=result_cd, result_msg=result_msg, result_dt=result_dt
    )

    if result_cd == constants.LOCAL_SEQUENCE_ALTERED:
        raise VSCUSequenceError(ctx)
    if result_cd == constants.LOCAL_INVOICE_ALREADY_PROCESSED:
        raise VSCUDuplicateSubmissionError(ctx)
    if result_cd in (constants.LOCAL_TRANSPORT_ERROR, "896"):
        raise VSCUUnavailableError(ctx)
    if result_cd in (
        constants.LOCAL_CLIENT_ERROR,
        constants.LOCAL_SALES_RECEIPT_TYPE_MISMATCH,
        constants.LOCAL_DATA_NOT_TRANSFERRED,
        constants.LOCAL_REPORT_NO_DATA,
        constants.LOCAL_REPORT_ALREADY_SENT,
    ):
        raise VSCUValidationError(ctx)

    try:
        code = int(result_cd)
    except ValueError:
        raise VSCUUnknownError(ctx) from None

    if code in constants.KRA_VALIDATION_RANGE or code in constants.KRA_REQUEST_ERROR_RANGE:
        raise VSCUValidationError(ctx)
    if code in constants.KRA_AUTH_RANGE:
        raise VSCUValidationError(ctx)
    if code in constants.KRA_BUSINESS_RANGE:
        raise VSCUValidationError(ctx)
    if code == constants.KRA_RATE_LIMIT or code in constants.KRA_TRANSIENT_SERVER_RANGE:
        raise VSCUUnavailableError(ctx)
    if code == constants.KRA_UNKNOWN_SERVER_ERROR:
        raise VSCUUnavailableError(ctx)
    if code == constants.KRA_DUPLICATE:
        raise VSCUDuplicateSubmissionError(ctx)
    raise VSCUUnknownError(ctx)


class VSCUClient:
    """One instance can serve many branches (each resolved to its own base_url via
    VscuDeviceState), same pattern as KraAdapter serving many branches from one shared
    httpx.AsyncClient — an absolute URL per request bypasses httpx's own base_url join.
    """

    def __init__(
        self,
        db: AsyncSession,
        http_client: httpx.AsyncClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._db = db
        self._settings = settings or get_settings()
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=self._settings.vscu_connect_timeout_seconds,
                read=self._settings.vscu_read_timeout_seconds,
                write=self._settings.vscu_read_timeout_seconds,
                pool=self._settings.vscu_connect_timeout_seconds,
            ),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _resolve_device(self, branch_id: uuid.UUID) -> VscuDeviceState:
        """VSCU has no shared global host (unlike kra_base_url) — every branch's device lives
        on that client's own network, so a VscuDeviceState row is mandatory, not an override
        of some default. Raises VSCUConfigurationError rather than silently falling back to
        Settings.vscu_base_url, which is dev/test-only.
        """
        if not self._settings.vscu_enabled:
            raise VSCUConfigurationError(
                VSCUErrorContext(endpoint="<config>"),
                message="VSCU integration is disabled (vscu_enabled=False).",
            )
        result = await self._db.execute(
            select(VscuDeviceState).where(VscuDeviceState.branch_id == branch_id)
        )
        device = result.scalar_one_or_none()
        if device is None:
            raise VSCUConfigurationError(
                VSCUErrorContext(endpoint="<config>"),
                message=(
                    f"No VscuDeviceState configured for branch {branch_id} — call initialize first."
                ),
            )
        return device

    async def _call(
        self,
        endpoint: str,
        payload: BaseModel,
        response_model: type[ResT],
        branch_id: uuid.UUID | None,
        correlation_id: uuid.UUID | None = None,
        base_url_override: str | None = None,
    ) -> ResT:
        """branch_id may be known with no VscuDeviceState row yet (the bootstrap
        /initializer/selectInitInfo call, before that row exists) — pass base_url_override in
        that case, same shape as KraAdapter's base_url_override for /selectInitOsdcInfo.
        """
        if base_url_override is not None:
            base_url = base_url_override.rstrip("/")
            timeout = httpx.Timeout(
                connect=self._settings.vscu_connect_timeout_seconds,
                read=self._settings.vscu_read_timeout_seconds,
                write=self._settings.vscu_read_timeout_seconds,
                pool=self._settings.vscu_connect_timeout_seconds,
            )
        else:
            assert branch_id is not None
            device = await self._resolve_device(branch_id)
            base_url = device.base_url.rstrip("/")
            timeout = httpx.Timeout(
                connect=device.connect_timeout_seconds
                or self._settings.vscu_connect_timeout_seconds,
                read=device.read_timeout_seconds or self._settings.vscu_read_timeout_seconds,
                write=device.read_timeout_seconds or self._settings.vscu_read_timeout_seconds,
                pool=device.connect_timeout_seconds or self._settings.vscu_connect_timeout_seconds,
            )
        url = f"{base_url}/{endpoint.lstrip('/')}"
        correlation_id = correlation_id or uuid.uuid4()

        body: dict[str, Any] = payload.model_dump(mode="json", exclude_none=True)
        request_hash = hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()

        start = time.monotonic()
        try:
            http_response = await self._client.post(url, json=body, timeout=timeout)
            http_response.raise_for_status()
        except httpx.HTTPError as exc:
            latency_ms = int((time.monotonic() - start) * 1000)
            if isinstance(exc, httpx.HTTPStatusError):
                synthetic_result_cd = f"HTTP_{exc.response.status_code}"
                synthetic_result_msg = f"HTTP {exc.response.status_code} calling {url}"
                http_status = exc.response.status_code
            else:
                synthetic_result_cd = "TRANSPORT_ERROR"
                synthetic_result_msg = f"{type(exc).__name__} calling {url}: {exc}"
                http_status = None

            await self._log_call(
                branch_id=branch_id,
                correlation_id=correlation_id,
                endpoint=endpoint,
                request_hash=request_hash,
                result_cd=synthetic_result_cd,
                result_msg=synthetic_result_msg,
                result_dt=None,
                http_status=http_status,
                latency_ms=latency_ms,
            )
            vscu_requests_total.labels(endpoint=endpoint, result_cd=synthetic_result_cd).inc()
            vscu_request_duration_seconds.labels(endpoint=endpoint).observe(latency_ms / 1000)

            raise VSCUTransportError(
                VSCUErrorContext(
                    endpoint=endpoint,
                    result_cd=synthetic_result_cd,
                    result_msg=synthetic_result_msg,
                    http_status=http_status,
                    correlation_id=str(correlation_id),
                )
            ) from exc

        latency_ms = int((time.monotonic() - start) * 1000)
        response = response_model.model_validate(http_response.json())

        await self._log_call(
            branch_id=branch_id,
            correlation_id=correlation_id,
            endpoint=endpoint,
            request_hash=request_hash,
            result_cd=response.resultCd,
            result_msg=response.resultMsg,
            result_dt=response.resultDt,
            http_status=http_response.status_code,
            latency_ms=latency_ms,
        )
        vscu_requests_total.labels(endpoint=endpoint, result_cd=response.resultCd).inc()
        vscu_request_duration_seconds.labels(endpoint=endpoint).observe(latency_ms / 1000)

        try:
            raise_for_result(endpoint, response.resultCd, response.resultMsg, response.resultDt)
        except VSCUError as err:
            err.context.correlation_id = str(correlation_id)
            raise
        return response

    async def _log_call(
        self,
        *,
        branch_id: uuid.UUID | None,
        correlation_id: uuid.UUID,
        endpoint: str,
        request_hash: str,
        result_cd: str | None,
        result_msg: str | None,
        result_dt: str | None,
        http_status: int | None,
        latency_ms: int,
    ) -> None:
        self._db.add(
            VscuCallLog(
                branch_id=branch_id,
                correlation_id=correlation_id,
                endpoint=endpoint,
                request_hash=request_hash,
                result_cd=result_cd,
                result_msg=result_msg,
                result_dt=result_dt,
                http_status=http_status,
                latency_ms=latency_ms,
            )
        )
        await self._db.flush()

    async def get_server_time(
        self, branch_id: uuid.UUID, correlation_id: uuid.UUID | None = None
    ) -> str:
        """GET /main/selectServerTime — returns a bare string (verified: MainExcute.java's
        method signature is `public String selectServerTime()`, not a JSON envelope).
        """
        device = await self._resolve_device(branch_id)
        url = f"{device.base_url.rstrip('/')}/main/selectServerTime"
        start = time.monotonic()
        try:
            http_response = await self._client.get(url)
            http_response.raise_for_status()
        except httpx.HTTPError as exc:
            latency_ms = int((time.monotonic() - start) * 1000)
            await self._log_call(
                branch_id=branch_id,
                correlation_id=correlation_id or uuid.uuid4(),
                endpoint="/main/selectServerTime",
                request_hash="",
                result_cd="TRANSPORT_ERROR",
                result_msg=str(exc),
                result_dt=None,
                http_status=getattr(getattr(exc, "response", None), "status_code", None),
                latency_ms=latency_ms,
            )
            raise VSCUTransportError(
                VSCUErrorContext(endpoint="/main/selectServerTime", result_msg=str(exc))
            ) from exc
        return http_response.text

    async def select_init_info(
        self,
        base_url: str,
        req: InitInfoReq,
        branch_id: uuid.UUID | None = None,
        correlation_id: uuid.UUID | None = None,
    ) -> InitInfoRes:
        """POST /initializer/selectInitInfo, called BEFORE any VscuDeviceState row exists for
        this branch — that row is what this call's result gets persisted into (see
        VSCUInitializationService.initialize). Verified: this proxies straight through to real
        KRA on every call (InitializeVsdcExcute.selectInitInfo calls
        this.apiClient.getClient(...) unconditionally) — it is not idempotent/free to poll.
        """
        return await self._call(
            "/initializer/selectInitInfo",
            req,
            InitInfoRes,
            branch_id=branch_id,
            correlation_id=correlation_id,
            base_url_override=base_url,
        )

    async def select_sequences(
        self,
        branch_id: uuid.UUID | None,
        req: InitInfoReq,
        correlation_id: uuid.UUID | None = None,
        base_url_override: str | None = None,
    ) -> InitInfoRes:
        """POST /initializer/selectSequences. Verified: InitializeVsdcExcute.selectSequences
        also proxies straight through to real KRA (calls selectTotReceiptNo, which hits
        upstream `selectInitInfoVsdcSeq`) — this is a live KRA round-trip, not a local-only
        read of cached state, despite the name.

        base_url_override: same bootstrap escape hatch as select_init_info — needed when
        VSCUInitializationService.initialize falls back to this call after a 902 "This device
        is installed" response, i.e. before any VscuDeviceState row exists to resolve a base
        URL from.
        """
        return await self._call(
            "/initializer/selectSequences",
            req,
            InitInfoRes,
            branch_id=branch_id,
            correlation_id=correlation_id,
            base_url_override=base_url_override,
        )

    async def update_sequences(
        self, branch_id: uuid.UUID, req: InitInfoReq, correlation_id: uuid.UUID | None = None
    ) -> InitInfoRes:
        """POST /initializer/updateSequences. Verified: purely local (SequenceUpdater."""
        return await self._call(
            "/initializer/updateSequences",
            req,
            InitInfoRes,
            branch_id=branch_id,
            correlation_id=correlation_id,
        )

    async def echo_test(
        self, branch_id: uuid.UUID, req: EchoTestReq, correlation_id: uuid.UUID | None = None
    ) -> EchoTestRes:
        """POST /test/echoTest. Verified: TestExcute.selectTestEcho proxies straight through
        to real KRA — a genuine end-to-end connectivity check, not a local-only ping.
        """
        return await self._call(
            "/test/echoTest", req, EchoTestRes, branch_id=branch_id, correlation_id=correlation_id
        )

    # --- Phase 3: reference data. Every method below is a verified plain proxy to real KRA
    # (no local business logic in the decompiled controller) except save_item, flagged on
    # ItemSaveRes's own docstring for its "always reports success to us" quirk. ---

    async def select_codes(
        self, branch_id: uuid.UUID, req: CodeReq, correlation_id: uuid.UUID | None = None
    ) -> CodeRes:
        return await self._call(
            "/code/selectCodes", req, CodeRes, branch_id=branch_id, correlation_id=correlation_id
        )

    async def select_item_classes(
        self, branch_id: uuid.UUID, req: ItemClsReq, correlation_id: uuid.UUID | None = None
    ) -> ItemClsRes:
        return await self._call(
            "/itemClass/selectItemsClass",
            req,
            ItemClsRes,
            branch_id=branch_id,
            correlation_id=correlation_id,
        )

    async def select_customer(
        self, branch_id: uuid.UUID, req: CustReq, correlation_id: uuid.UUID | None = None
    ) -> CustRes:
        return await self._call(
            "/customers/selectCustomer",
            req,
            CustRes,
            branch_id=branch_id,
            correlation_id=correlation_id,
        )

    async def select_branches(
        self, branch_id: uuid.UUID, req: BhfReq, correlation_id: uuid.UUID | None = None
    ) -> BhfRes:
        return await self._call(
            "/branches/selectBranches",
            req,
            BhfRes,
            branch_id=branch_id,
            correlation_id=correlation_id,
        )

    async def save_branch_user(
        self, branch_id: uuid.UUID, req: BhfUserSaveReq, correlation_id: uuid.UUID | None = None
    ) -> BhfUserSaveRes:
        return await self._call(
            "/branches/saveBrancheUsers",
            req,
            BhfUserSaveRes,
            branch_id=branch_id,
            correlation_id=correlation_id,
        )

    async def save_branch_insurance(
        self,
        branch_id: uuid.UUID,
        req: BhfInsuranceSaveReq,
        correlation_id: uuid.UUID | None = None,
    ) -> BhfInsuranceSaveRes:
        return await self._call(
            "/branches/saveBrancheInsurances",
            req,
            BhfInsuranceSaveRes,
            branch_id=branch_id,
            correlation_id=correlation_id,
        )

    async def save_branch_customer(
        self, branch_id: uuid.UUID, req: BhfCustSaveReq, correlation_id: uuid.UUID | None = None
    ) -> BhfCustSaveRes:
        return await self._call(
            "/branches/saveBrancheCustomers",
            req,
            BhfCustSaveRes,
            branch_id=branch_id,
            correlation_id=correlation_id,
        )

    async def select_items(
        self, branch_id: uuid.UUID, req: ItemReq, correlation_id: uuid.UUID | None = None
    ) -> ItemRes:
        return await self._call(
            "/items/selectItems", req, ItemRes, branch_id=branch_id, correlation_id=correlation_id
        )

    async def save_item(
        self, branch_id: uuid.UUID, req: ItemSaveReq, correlation_id: uuid.UUID | None = None
    ) -> ItemSaveRes:
        """POST /items/saveItems. See app/integrations/vscu/schemas/item.py's module
        docstring — resultCd=="000" here does NOT prove real KRA accepted the item, only that
        the local VSCU app queued the request. Never treat this as fiscal-grade confirmation.
        """
        return await self._call(
            "/items/saveItems",
            req,
            ItemSaveRes,
            branch_id=branch_id,
            correlation_id=correlation_id,
        )

    async def save_item_composition(
        self,
        branch_id: uuid.UUID,
        req: ItemCompositionSaveReq,
        correlation_id: uuid.UUID | None = None,
    ) -> ItemCompositionSaveRes:
        return await self._call(
            "/items/saveItemComposition",
            req,
            ItemCompositionSaveRes,
            branch_id=branch_id,
            correlation_id=correlation_id,
        )

    async def select_notices(
        self, branch_id: uuid.UUID, req: NoticeReq, correlation_id: uuid.UUID | None = None
    ) -> NoticeRes:
        return await self._call(
            "/notices/selectNotices",
            req,
            NoticeRes,
            branch_id=branch_id,
            correlation_id=correlation_id,
        )

    async def select_import_items(
        self, branch_id: uuid.UUID, req: ImportItemReq, correlation_id: uuid.UUID | None = None
    ) -> ImportItemRes:
        return await self._call(
            "/imports/selectImportItems",
            req,
            ImportItemRes,
            branch_id=branch_id,
            correlation_id=correlation_id,
        )

    async def update_import_item(
        self,
        branch_id: uuid.UUID,
        req: ImportItemUpdateReq,
        correlation_id: uuid.UUID | None = None,
    ) -> ImportItemUpdateRes:
        return await self._call(
            "/imports/updateImportItems",
            req,
            ImportItemUpdateRes,
            branch_id=branch_id,
            correlation_id=correlation_id,
        )

    # --- Phase 4: sales fiscalization ---

    async def save_sales(
        self, branch_id: uuid.UUID, req: TrnsSalesSaveWrReq, correlation_id: uuid.UUID | None = None
    ) -> TrnsSalesSaveWrRes:
        """POST /trnsSales/saveSales — see app/integrations/vscu/schemas/sales.py and
        TrnsSalesExcute.saveTrnsSales's decompiled logic: on a transport failure to real KRA
        (either exec_mode=OFFLINE, which never even tries, or exec_mode=ONLINE with KRA
        unreachable) the local app signs the receipt itself, queues it for its own resend
        scheduler, and unconditionally rewrites the result to resultCd="000" with
        resultMsg="INVOICE IS SIGNED BUT NOT TRANSMITTED" before ever returning to us — so a
        caller here only ever sees a non-"000" resultCd for a genuine local rejection (834
        bad salesTyCd/rcptTyCd, 836 sequence altered, 924 duplicate invoice, 802 backlog not
        yet drained, or 899 on an exception in the local app's own request handling), never
        for "KRA was unreachable" on its own. See VSCUSalesService.save_sale for how the
        "signed but not transmitted" case is persisted.
        """
        return await self._call(
            "/trnsSales/saveSales",
            req,
            TrnsSalesSaveWrRes,
            branch_id=branch_id,
            correlation_id=correlation_id,
        )

    async def select_invoice_details(
        self,
        branch_id: uuid.UUID,
        req: InvoiceDetailsReq,
        correlation_id: uuid.UUID | None = None,
    ) -> InvoiceDetailsRes:
        return await self._call(
            "/trnsSales/selectInvoiceDetails",
            req,
            InvoiceDetailsRes,
            branch_id=branch_id,
            correlation_id=correlation_id,
        )

    async def select_trns_sales_list(
        self, branch_id: uuid.UUID, req: TrnsSalesListReq, correlation_id: uuid.UUID | None = None
    ) -> InvoicesRes:
        return await self._call(
            "/trnsSales/selectTrnsSalesList",
            req,
            InvoicesRes,
            branch_id=branch_id,
            correlation_id=correlation_id,
        )

    # --- Phase 5: purchases and stock ---

    async def select_trns_purchase_sales(
        self,
        branch_id: uuid.UUID,
        req: TrnsPurchaseSalesReq,
        correlation_id: uuid.UUID | None = None,
    ) -> TrnsPurchaseSalesRes:
        return await self._call(
            "/trnsPurchase/selectTrnsPurchaseSales",
            req,
            TrnsPurchaseSalesRes,
            branch_id=branch_id,
            correlation_id=correlation_id,
        )

    async def save_purchases(
        self,
        branch_id: uuid.UUID,
        req: TrnsPurchaseSaveReq,
        correlation_id: uuid.UUID | None = None,
    ) -> TrnsPurchaseSaveRes:
        """POST /trnsPurchase/savePurchases. Verified: a plain proxy (TrnsPurchaseExcute has
        no local rewrite logic) — real KRA's resultCd is trusted as-is, unlike
        /items/saveItems or /stock/saveStockItems.
        """
        return await self._call(
            "/trnsPurchase/savePurchases",
            req,
            TrnsPurchaseSaveRes,
            branch_id=branch_id,
            correlation_id=correlation_id,
        )

    async def select_stock_items(
        self, branch_id: uuid.UUID, req: StockMoveReq, correlation_id: uuid.UUID | None = None
    ) -> StockMoveRes:
        return await self._call(
            "/stock/selectStockItems",
            req,
            StockMoveRes,
            branch_id=branch_id,
            correlation_id=correlation_id,
        )

    async def save_stock_items(
        self, branch_id: uuid.UUID, req: StockIoSaveReq, correlation_id: uuid.UUID | None = None
    ) -> StockIoSaveRes:
        """POST /stock/saveStockItems. See app/integrations/vscu/schemas/stock.py's module
        docstring — resultCd=="000" here does not prove real KRA accepted the movement, only
        that the local VSCU app queued the request.
        """
        return await self._call(
            "/stock/saveStockItems",
            req,
            StockIoSaveRes,
            branch_id=branch_id,
            correlation_id=correlation_id,
        )

    async def save_stock_master(
        self, branch_id: uuid.UUID, req: StockMasterSaveReq, correlation_id: uuid.UUID | None = None
    ) -> StockMasterSaveRes:
        """POST /stockMaster/saveStockMaster. Same "always reports 000 to us" quirk as
        save_stock_items — see that method's docstring.
        """
        return await self._call(
            "/stockMaster/saveStockMaster",
            req,
            StockMasterSaveRes,
            branch_id=branch_id,
            correlation_id=correlation_id,
        )

    # --- Z-reports ---

    async def save_z_report(
        self, branch_id: uuid.UUID, req: ReportZSaveReq, correlation_id: uuid.UUID | None = None
    ) -> ReportZSaveRes:
        """POST /reports/saveZReports. Verified: the local VSCU app builds the actual report
        body itself from its own tracked receipt history — we only ever send {tin, bhfId,
        rptDe}, never a report payload. See app/integrations/vscu/schemas/report.py.
        """
        return await self._call(
            "/reports/saveZReports",
            req,
            ReportZSaveRes,
            branch_id=branch_id,
            correlation_id=correlation_id,
        )

    async def check_z_report(
        self, branch_id: uuid.UUID, req: ReportZCheckReq, correlation_id: uuid.UUID | None = None
    ) -> ReportZCheckRes:
        return await self._call(
            "/reports/checkZReport",
            req,
            ReportZCheckRes,
            branch_id=branch_id,
            correlation_id=correlation_id,
        )
