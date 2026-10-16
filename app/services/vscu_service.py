"""VSCUInitializationService / VSCUTestService: the application-level operations behind
app/api/v1/endpoints/vscu.py. Mirrors app/services/cmc_key_manager.py's shape (thin service
functions around the transport client, one Branch row already assumed to exist) but for the
VSCU integration layer — see app/integrations/vscu/client.py for why this isn't KraAdapter.
"""

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Branch, VscuDeviceState
from app.integrations.vscu import constants
from app.integrations.vscu.client import VSCUClient
from app.integrations.vscu.exceptions import (
    VSCUConfigurationError,
    VSCUErrorContext,
    VSCUValidationError,
)
from app.integrations.vscu.schemas.init import InitInfoReq, InitInfoRes
from app.integrations.vscu.schemas.test import EchoTestReq, EchoTestRes

logger = logging.getLogger(__name__)


async def _get_branch(db: AsyncSession, branch_id: uuid.UUID) -> Branch:
    branch = await db.get(Branch, branch_id)
    if branch is None:
        raise ValueError(f"No branch found for branch_id={branch_id}")
    return branch


class VSCUInitializationService:
    """initialize() is the only place a VscuDeviceState row is created — every other VSCU
    client method requires one to already exist (app/integrations/vscu/client.py's
    _resolve_device raises VSCUConfigurationError otherwise).
    """

    def __init__(self, db: AsyncSession, client: VSCUClient) -> None:
        self._db = db
        self._client = client

    async def initialize(
        self,
        branch_id: uuid.UUID,
        base_url: str,
        dvc_srl_no: str | None = None,
        connect_timeout_seconds: float | None = None,
        read_timeout_seconds: float | None = None,
        correlation_id: uuid.UUID | None = None,
    ) -> VscuDeviceState:
        """POST /initializer/selectInitInfo against the given base_url, then persist the
        result as this branch's VscuDeviceState (upsert — re-running initialize, e.g. after
        the client moves their VSCU device to a new local IP, is expected and safe: it's a
        live KRA round-trip either way, never a local-only no-op).

        dvc_srl_no: KRA issues one domain-wide serial number per integration (not per-branch,
        not per-customer) — omit to use Settings.vscu_dvc_srl_no_for_env(); pass explicitly
        only to override that default for one device.

        If the device already completed initialization for this (tin, bhfId) — confirmed
        against a real device (2026-09-02) that selectInitInfo is not re-callable/idempotent,
        responding 902 "This device is installed" on a second call — this falls back to
        /initializer/selectSequences, which returns the same InitInfoVO-shaped payload for an
        already-initialized device, and persists that instead of raising.
        """
        if dvc_srl_no is None:
            try:
                dvc_srl_no = get_settings().vscu_dvc_srl_no_for_env()
            except ValueError as exc:
                # Not a plain ValueError (the endpoint's ValueError catch means "branch not
                # found" -> 404, which would misreport a config problem as a 404) — no request
                # was ever attempted, matching VSCUConfigurationError's own contract.
                raise VSCUConfigurationError(
                    VSCUErrorContext(endpoint="/initializer/selectInitInfo"), message=str(exc)
                ) from exc

        branch = await _get_branch(self._db, branch_id)
        req = InitInfoReq(tin=branch.tin, bhfId=branch.bhf_id, dvcSrlNo=dvc_srl_no)

        res: InitInfoRes
        try:
            res = await self._client.select_init_info(
                base_url, req, branch_id=branch_id, correlation_id=correlation_id
            )
        except VSCUValidationError as exc:
            if exc.context.result_cd != constants.KRA_DEVICE_ALREADY_INSTALLED:
                raise
            logger.info(
                "VSCU branch_id=%s already initialized (902 on selectInitInfo); falling back "
                "to selectSequences to fetch its current state",
                branch_id,
            )
            res = await self._client.select_sequences(
                branch_id, req, correlation_id=correlation_id, base_url_override=base_url
            )
        if res.data is None or res.data.info is None:
            raise VSCUConfigurationError(
                VSCUErrorContext(
                    endpoint="/initializer/selectInitInfo",
                    result_cd=res.resultCd,
                    result_msg=res.resultMsg,
                    result_dt=res.resultDt,
                ),
                message="VSCU selectInitInfo returned resultCd=000 with no data.info",
            )
        info = res.data.info

        existing = await self._db.scalar(
            select(VscuDeviceState).where(VscuDeviceState.branch_id == branch_id)
        )
        now = datetime.now(UTC)
        if existing is None:
            existing = VscuDeviceState(branch_id=branch_id)
            self._db.add(existing)

        existing.base_url = base_url
        existing.connect_timeout_seconds = connect_timeout_seconds
        existing.read_timeout_seconds = read_timeout_seconds
        existing.dvc_srl_no = dvc_srl_no
        existing.sdc_id = info.sdcId
        existing.mrc_no = info.mrcNo
        existing.dvc_id = info.dvcId
        existing.vat_ty_cd = info.vatTyCd
        existing.taxpr_nm = info.taxprNm
        existing.bhf_nm = info.bhfNm
        existing.initialized_at = now
        existing.last_known_invc_no = info.lastInvcNo
        existing.last_known_sale_rcpt_no = info.lastSaleRcptNo
        existing.last_successful_call_at = now
        existing.last_error = None

        await self._db.flush()
        logger.info("VSCU initialized for branch_id=%s dvc_srl_no=%s", branch_id, dvc_srl_no)
        return existing

    async def sync_sequences(
        self, branch_id: uuid.UUID, correlation_id: uuid.UUID | None = None
    ) -> InitInfoRes:
        """POST /initializer/selectSequences — a live KRA round-trip (verified, see
        client.select_sequences), not a local cache refresh. Updates our own visibility cache
        only; VSCU's local H2 store remains the authoritative sequence owner.
        """
        branch = await _get_branch(self._db, branch_id)
        device = await self._require_device_state(branch_id)
        req = InitInfoReq(tin=branch.tin, bhfId=branch.bhf_id, dvcSrlNo=device.dvc_srl_no or "")

        res = await self._client.select_sequences(branch_id, req, correlation_id=correlation_id)
        if res.data is not None and res.data.info is not None:
            device.last_known_invc_no = res.data.info.lastInvcNo
            device.last_known_sale_rcpt_no = res.data.info.lastSaleRcptNo
        device.last_sequence_sync_at = datetime.now(UTC)
        device.last_successful_call_at = datetime.now(UTC)
        await self._db.flush()
        return res

    async def update_sequences(
        self, branch_id: uuid.UUID, correlation_id: uuid.UUID | None = None
    ) -> InitInfoRes:
        """POST /initializer/updateSequences — purely local (SequenceUpdater.processSequences,
        no KRA round-trip). Use this to reconcile the local VSCU app's own sequence state
        after a 836 (VSCUSequenceError), per spec §9's reconciliation flow — never as a
        substitute for sync_sequences when you actually need KRA's authoritative numbers.
        """
        branch = await _get_branch(self._db, branch_id)
        device = await self._require_device_state(branch_id)
        req = InitInfoReq(tin=branch.tin, bhfId=branch.bhf_id, dvcSrlNo=device.dvc_srl_no or "")
        res = await self._client.update_sequences(branch_id, req, correlation_id=correlation_id)
        device.last_successful_call_at = datetime.now(UTC)
        await self._db.flush()
        return res

    async def _require_device_state(self, branch_id: uuid.UUID) -> VscuDeviceState:
        device = await self._db.scalar(
            select(VscuDeviceState).where(VscuDeviceState.branch_id == branch_id)
        )
        if device is None:
            raise VSCUConfigurationError(
                VSCUErrorContext(endpoint="<config>", result_msg=f"branch_id={branch_id}"),
                message=f"No VscuDeviceState for branch {branch_id} — call initialize first.",
            )
        return device


class VSCUTestService:
    """Connectivity checks. echo() is a genuine end-to-end check (proxies to real KRA,
    verified) — server_time() only proves the local VSCU app process itself is reachable and
    responding, since /main/selectServerTime's own upstream failure just yields an empty
    string rather than an error (MainExcute.java: `catch (Exception e) { resStr = ""; }`), so
    an empty response does not by itself prove KRA connectivity.
    """

    def __init__(self, db: AsyncSession, client: VSCUClient) -> None:
        self._db = db
        self._client = client

    async def echo(
        self, branch_id: uuid.UUID, test_str: str = "PING", correlation_id: uuid.UUID | None = None
    ) -> EchoTestRes:
        branch = await _get_branch(self._db, branch_id)
        req = EchoTestReq(tin=branch.tin, bhfId=branch.bhf_id, testStr=test_str)
        return await self._client.echo_test(branch_id, req, correlation_id=correlation_id)

    async def server_time(
        self, branch_id: uuid.UUID, correlation_id: uuid.UUID | None = None
    ) -> str:
        return await self._client.get_server_time(branch_id, correlation_id=correlation_id)
