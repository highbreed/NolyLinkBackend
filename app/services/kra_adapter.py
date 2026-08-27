"""KraAdapter: httpx.AsyncClient wrapper, one method per KRA endpoint (spec §5).

Scope boundary: this module owns *transport*-level concerns (base URL selection, request
timeout, low-level connection retries, resultCd -> exception mapping, and call logging). It
does NOT retry on business-level transient/rate-limit errors (KraRateLimitError,
KraTransientServerError) — that backoff-and-retry loop is the outbox_dispatcher's job
(spec §14, Phase 8), since only the outbox knows whether a call is safe to resubmit.
"""

import hashlib
import json
import uuid
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.metrics import kra_calls_total
from app.db.models import Branch, KraCallLog
from app.schemas.kra.branch import (
    BhfCustSaveReq,
    BhfCustSaveRes,
    BhfInsuranceSaveReq,
    BhfInsuranceSaveRes,
    BhfSearchReq,
    BhfSearchRes,
    BhfUserSaveReq,
    BhfUserSaveRes,
)
from app.schemas.kra.codes import (
    CodeSearchReq,
    CodeSearchRes,
    CustSearchReq,
    CustSearchRes,
    NoticeSearchReq,
    NoticeSearchRes,
)
from app.schemas.kra.common import KraResponseEnvelope
from app.schemas.kra.device import DeviceVerificationReq, DeviceVerificationRes
from app.schemas.kra.import_item import (
    ImportItemSearchReq,
    ImportItemSearchRes,
    ImportItemUpdateReq,
    ImportItemUpdateRes,
)
from app.schemas.kra.item import (
    ItemClsSearchReq,
    ItemClsSearchRes,
    ItemSaveReq,
    ItemSaveRes,
    ItemSearchReq,
    ItemSearchRes,
)
from app.schemas.kra.purchase import (
    TrnsPurchaseSalesReq,
    TrnsPurchaseSalesRes,
    TrnsPurchaseSaveReq,
    TrnsPurchaseSaveRes,
)
from app.schemas.kra.sales import TrnsSalesSaveWrReq, TrnsSalesSaveWrRes
from app.schemas.kra.stock import (
    StockIOSaveReq,
    StockIOSaveRes,
    StockMasterSaveReq,
    StockMasterSaveRes,
    StockMoveReq,
    StockMoveRes,
)


class KraError(Exception):
    """Base for every error mapped from a non-000/001 KRA resultCd (spec §10)."""

    def __init__(self, result_cd: str, result_msg: str, result_dt: str) -> None:
        self.result_cd = result_cd
        self.result_msg = result_msg
        self.result_dt = result_dt
        super().__init__(f"KRA {result_cd}: {result_msg}")


class KraValidationError(KraError):
    """891-899 (transport/format errors) and 910-912 (request parameter/body/method errors).
    A bug in our adapter/payload — log at ERROR, alert, do not retry unmodified.
    """


class KraAuthError(KraError):
    """900-903: no header info / invalid device / already installed / non-OSCU device."""


class KraBusinessError(KraError):
    """921-922: sales-invoice business-rule/sequencing errors — surface to client as 409."""


class KraRateLimitError(KraError):
    """990: max views exceeded — backoff and retry per the polling cadence."""


class KraTransientServerError(KraError):
    """991-993 (registration/modification/deletion error) and 999 (unknown) — 5xx-class,
    retry with exponential backoff.
    """


class KraDuplicateError(KraError):
    """994: overlapped data — success-equivalent if our idempotency key already matches a
    prior success, else a conflict.
    """


class KraUnknownError(KraError):
    """995 (no downloaded file, N/A for our endpoints) or any resultCd not in spec §10's table."""


def raise_for_result(result_cd: str, result_msg: str, result_dt: str) -> None:
    """Raise the matching KraError subclass for a non-success resultCd (spec §10).

    "000" (success) and "001" (no search result — an empty result set, not an error, for
    GET-style polling) both return normally.
    """
    if result_cd in ("000", "001"):
        return

    try:
        code = int(result_cd)
    except ValueError:
        raise KraUnknownError(result_cd, result_msg, result_dt) from None

    if 891 <= code <= 899 or 910 <= code <= 912:
        raise KraValidationError(result_cd, result_msg, result_dt)
    if 900 <= code <= 903:
        raise KraAuthError(result_cd, result_msg, result_dt)
    if 921 <= code <= 922:
        raise KraBusinessError(result_cd, result_msg, result_dt)
    if code == 990:
        raise KraRateLimitError(result_cd, result_msg, result_dt)
    if 991 <= code <= 993 or code == 999:
        raise KraTransientServerError(result_cd, result_msg, result_dt)
    if code == 994:
        raise KraDuplicateError(result_cd, result_msg, result_dt)
    raise KraUnknownError(result_cd, result_msg, result_dt)


ResT = TypeVar("ResT", bound=KraResponseEnvelope[Any])


class _RawKraRequest(BaseModel):
    """Permissive passthrough model for endpoints with no Pydantic schema yet (currently
    only save_item_composition — see spec §5.1).
    """

    model_config = {"extra": "allow"}


class KraAdapter:
    """One method per KRA endpoint in spec §5's table. Every call is logged to kra_call_log
    (never the raw payload, which carries cmcKey and PII — only its hash) and raises a
    KraError subclass for any non-000/001 resultCd.
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
            base_url=self._settings.kra_base_url,
            timeout=self._settings.kra_request_timeout_seconds,
            transport=httpx.AsyncHTTPTransport(retries=2),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _resolve_base_url(
        self,
        branch_id: uuid.UUID | None,
        base_url_override: str | None,
        device_type: str | None,
    ) -> str:
        """OSCU and VSCU devices share this exact request/response contract (spec-confirmed) —
        the only thing that differs per branch is which host to call. Precedence:
        base_url_override (an explicit per-call/per-branch pin) > the branch's own
        kra_base_url_override column (steady-state calls, resolved via branch_id) >
        device_type's global default (only relevant for /selectInitOsdcInfo, called before any
        Branch row exists) > the plain OSCU global default.

        Always resolves through self._settings (never the module-level get_settings()), so a
        KraAdapter constructed with injected test settings never leaks into a real config.
        """
        if base_url_override:
            return base_url_override
        if branch_id is not None:
            branch = await self._db.get(Branch, branch_id)
            if branch is not None:
                if branch.kra_base_url_override:
                    return branch.kra_base_url_override
                return self._settings.kra_base_url_for(branch.device_type)
        if device_type is not None:
            return self._settings.kra_base_url_for(device_type)
        return self._settings.kra_base_url

    async def _call(
        self,
        endpoint: str,
        payload: BaseModel,
        response_model: type[ResT],
        branch_id: uuid.UUID | None = None,
        base_url_override: str | None = None,
        device_type: str | None = None,
    ) -> ResT:
        body: dict[str, Any] = payload.model_dump(mode="json", exclude_none=True)
        request_hash = hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()

        base_url = await self._resolve_base_url(branch_id, base_url_override, device_type)
        # An absolute URL bypasses self._client's own configured base_url entirely (httpx only
        # joins relative URLs against it) — this is what lets one adapter/client instance serve
        # different branches pointed at different hosts (needed by outbox_dispatcher, which
        # dispatches rows for many branches through one shared KraAdapter).
        url = f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        http_response = await self._client.post(url, json=body)
        http_response.raise_for_status()
        response = response_model.model_validate(http_response.json())

        result_cd = response.resultCd
        result_msg = response.resultMsg
        result_dt = response.resultDt

        self._db.add(
            KraCallLog(
                branch_id=branch_id,
                endpoint=endpoint,
                request_hash=request_hash,
                result_cd=result_cd,
                result_msg=result_msg,
                result_dt=result_dt,
            )
        )
        await self._db.flush()

        kra_calls_total.labels(endpoint=endpoint, result_cd=result_cd).inc()

        raise_for_result(result_cd, result_msg, result_dt)
        return response

    async def initialize_device(
        self,
        req: DeviceVerificationReq,
        branch_id: uuid.UUID | None = None,
        base_url_override: str | None = None,
        device_type: str = "OSCU",
    ) -> DeviceVerificationRes:
        # The only KRA call ever made before a Branch row exists — device_type/
        # base_url_override let the caller target OSCU vs VSCU (or a custom host) directly,
        # since there's no branch to resolve it from yet.
        return await self._call(
            "/selectInitOsdcInfo",
            req,
            DeviceVerificationRes,
            branch_id=branch_id,
            base_url_override=base_url_override,
            device_type=device_type,
        )

    async def get_code_list(
        self, req: CodeSearchReq, branch_id: uuid.UUID | None = None
    ) -> CodeSearchRes:
        return await self._call("/selectCodeList", req, CodeSearchRes, branch_id=branch_id)

    async def get_item_class_list(
        self, req: ItemClsSearchReq, branch_id: uuid.UUID | None = None
    ) -> ItemClsSearchRes:
        return await self._call("/selectItemClsList", req, ItemClsSearchRes, branch_id=branch_id)

    async def get_customer(
        self, req: CustSearchReq, branch_id: uuid.UUID | None = None
    ) -> CustSearchRes:
        return await self._call("/selectCustomer", req, CustSearchRes, branch_id=branch_id)

    async def get_branch_list(
        self, req: BhfSearchReq, branch_id: uuid.UUID | None = None
    ) -> BhfSearchRes:
        return await self._call("/selectBhfList", req, BhfSearchRes, branch_id=branch_id)

    async def get_notice_list(
        self, req: NoticeSearchReq, branch_id: uuid.UUID | None = None
    ) -> NoticeSearchRes:
        return await self._call("/selectNoticeList", req, NoticeSearchRes, branch_id=branch_id)

    async def save_branch_customer(
        self, req: BhfCustSaveReq, branch_id: uuid.UUID | None = None
    ) -> BhfCustSaveRes:
        return await self._call("/saveBhfCustomer", req, BhfCustSaveRes, branch_id=branch_id)

    async def save_branch_user(
        self, req: BhfUserSaveReq, branch_id: uuid.UUID | None = None
    ) -> BhfUserSaveRes:
        return await self._call("/saveBhfUser", req, BhfUserSaveRes, branch_id=branch_id)

    async def save_branch_insurance(
        self, req: BhfInsuranceSaveReq, branch_id: uuid.UUID | None = None
    ) -> BhfInsuranceSaveRes:
        return await self._call("/saveBhfInsurance", req, BhfInsuranceSaveRes, branch_id=branch_id)

    async def get_item_list(
        self, req: ItemSearchReq, branch_id: uuid.UUID | None = None
    ) -> ItemSearchRes:
        return await self._call("/selectItemList", req, ItemSearchRes, branch_id=branch_id)

    async def save_item(self, req: ItemSaveReq, branch_id: uuid.UUID | None = None) -> ItemSaveRes:
        return await self._call("/saveItem", req, ItemSaveRes, branch_id=branch_id)

    async def save_item_composition(
        self, payload: dict[str, Any], branch_id: uuid.UUID | None = None
    ) -> KraResponseEnvelope[dict[str, Any]]:
        """No Pydantic schema exists for this payload — spec §6 never documents item
        composition's fields, and §5.1 flags even the endpoint URL as unconfirmed. Takes a
        raw dict and the generic envelope until KRA's Swagger/sandbox response confirms both.
        """
        req = _RawKraRequest.model_validate(payload)
        return await self._call(
            self._settings.kra_item_composition_endpoint,
            req,
            KraResponseEnvelope[dict[str, Any]],
            branch_id=branch_id,
        )

    async def get_import_item_list(
        self, req: ImportItemSearchReq, branch_id: uuid.UUID | None = None
    ) -> ImportItemSearchRes:
        return await self._call(
            "/selectImportItemList", req, ImportItemSearchRes, branch_id=branch_id
        )

    async def update_import_item(
        self, req: ImportItemUpdateReq, branch_id: uuid.UUID | None = None
    ) -> ImportItemUpdateRes:
        return await self._call("/updateImportItem", req, ImportItemUpdateRes, branch_id=branch_id)

    async def save_sales_transaction(
        self, req: TrnsSalesSaveWrReq, branch_id: uuid.UUID | None = None
    ) -> TrnsSalesSaveWrRes:
        return await self._call("/saveTrnsSalesOsdc", req, TrnsSalesSaveWrRes, branch_id=branch_id)

    async def get_purchase_sales_list(
        self, req: TrnsPurchaseSalesReq, branch_id: uuid.UUID | None = None
    ) -> TrnsPurchaseSalesRes:
        return await self._call(
            "/selectTrnsPurchaseSalesList", req, TrnsPurchaseSalesRes, branch_id=branch_id
        )

    async def save_purchase_transaction(
        self, req: TrnsPurchaseSaveReq, branch_id: uuid.UUID | None = None
    ) -> TrnsPurchaseSaveRes:
        return await self._call(
            "/insertTrnsPurchase", req, TrnsPurchaseSaveRes, branch_id=branch_id
        )

    async def get_stock_move_list(
        self, req: StockMoveReq, branch_id: uuid.UUID | None = None
    ) -> StockMoveRes:
        return await self._call("/selectStockMoveList", req, StockMoveRes, branch_id=branch_id)

    async def save_stock_io(
        self, req: StockIOSaveReq, branch_id: uuid.UUID | None = None
    ) -> StockIOSaveRes:
        return await self._call("/insertStockIO", req, StockIOSaveRes, branch_id=branch_id)

    async def save_stock_master(
        self, req: StockMasterSaveReq, branch_id: uuid.UUID | None = None
    ) -> StockMasterSaveRes:
        return await self._call("/saveStockMaster", req, StockMasterSaveRes, branch_id=branch_id)
