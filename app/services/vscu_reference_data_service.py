"""VSCUReferenceDataService: codes/item-classes/customers/branches/items/notices/import-items
over the VSCU integration layer. Mirrors app/services/code_cache_service.py,
app/services/item_class_cache_service.py, app/services/notice_service.py, and
app/services/import_item_service.py's shape — same cache tables (KraCodeList, KraItemClass,
NoticeCache, ImportItemCache) and the same sync_framework resource keys ("codes",
"item_classes", "notices", "import_items") as their OSCU equivalents, since this is the same
KRA reference data either way; only the transport (VSCUClient vs. KraAdapter) differs. A branch
is always exclusively OSCU or VSCU (Branch.device_type), so there's no risk of the two
transports racing to sync the same (tin, bhfId, resource) watermark.

Customer lookup and branch reference calls are NOT cached, per spec guidance: don't overwrite
SAP customer/branch master data automatically. Item save (save_item) is a verified quirk case
— see app/integrations/vscu/schemas/item.py's module docstring before trusting its resultCd.
"""

import logging
import uuid
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import Branch, ImportItemCache, NoticeCache
from app.integrations.vscu.client import VSCUClient
from app.integrations.vscu.exceptions import VSCUError
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
from app.integrations.vscu.schemas.codes import CodeReq
from app.integrations.vscu.schemas.customer import CustReq, CustRes
from app.integrations.vscu.schemas.import_item import (
    ImportItemReq,
    ImportItemUpdateReq,
    ImportItemUpdateRes,
)
from app.integrations.vscu.schemas.item import (
    Item,
    ItemCompositionSaveReq,
    ItemCompositionSaveRes,
    ItemReq,
    ItemRes,
    ItemSaveReq,
    ItemSaveRes,
)
from app.integrations.vscu.schemas.item_class import ItemClsReq, ItemClsRes
from app.integrations.vscu.schemas.notice import NoticeReq
from app.services import sync_framework
from app.services.code_cache_service import _upsert_code, _upsert_code_class
from app.services.item_class_cache_service import _upsert_item_class

logger = logging.getLogger(__name__)

CODES_RESOURCE = "codes"
NOTICES_RESOURCE = "notices"
IMPORT_ITEMS_RESOURCE = "import_items"
ITEM_CLASSES_RESOURCE = "item_classes"


class VSCUReferenceDataService:
    def __init__(self, db: AsyncSession, client: VSCUClient) -> None:
        self._db = db
        self._client = client

    async def sync_codes(self, branch: Branch, correlation_id: uuid.UUID | None = None) -> None:
        """POST /code/selectCodes, upserted into kra_code_lists — same table/shape OSCU's
        code_cache_service.refresh_code_list writes to (spec-confirmed identical field shape).
        """
        last_req_dt = await sync_framework.get_last_req_dt(
            self._db, branch.tin, branch.bhf_id, CODES_RESOURCE
        )
        req = CodeReq(tin=branch.tin, bhfId=branch.bhf_id, lastReqDt=last_req_dt)
        res = await self._client.select_codes(branch.id, req, correlation_id=correlation_id)

        if res.data is not None:
            for cls in res.data.clsList:
                await _upsert_code_class(
                    self._db, cd_cls=cls.cdCls, cd_cls_nm=cls.cdClsNm, cd_cls_desc=cls.cdClsDesc
                )
                for detail in cls.dtlList:
                    await _upsert_code(
                        self._db,
                        cd_cls=cls.cdCls,
                        cd=detail.cd,
                        cd_nm=detail.cdNm,
                        cd_desc=detail.cdDesc,
                        srt_ord=detail.srtOrd,
                        use_yn=detail.useYn,
                    )

        if res.resultDt:
            await sync_framework.advance_last_req_dt(
                self._db, branch.tin, branch.bhf_id, CODES_RESOURCE, res.resultDt
            )

    async def select_item_classes(
        self, branch: Branch, last_req_dt: str, correlation_id: uuid.UUID | None = None
    ) -> ItemClsRes:
        """Also persists into kra_item_classes (app/services/item_class_cache_service.py) —
        same table and "item_classes" sync_framework resource key OSCU's
        item_class_cache_service.refresh_item_classes writes to, keeping this at parity with
        sync_codes above rather than leaving item classes as the one uncached VSCU resource.
        """
        req = ItemClsReq(tin=branch.tin, bhfId=branch.bhf_id, lastReqDt=last_req_dt)
        res = await self._client.select_item_classes(branch.id, req, correlation_id=correlation_id)

        if res.data is not None:
            for item_cls in res.data.itemClsList:
                await _upsert_item_class(
                    self._db,
                    item_cls_cd=item_cls.itemClsCd,
                    item_cls_nm=item_cls.itemClsNm,
                    item_cls_lvl=None if item_cls.itemClsLvl is None else str(item_cls.itemClsLvl),
                    tax_ty_cd=item_cls.taxTyCd,
                    mjr_tg_yn=item_cls.mjrTgYn,
                    use_yn=item_cls.useYn,
                )

        if res.resultDt:
            await sync_framework.advance_last_req_dt(
                self._db, branch.tin, branch.bhf_id, ITEM_CLASSES_RESOURCE, res.resultDt
            )
        return res

    async def lookup_customer(
        self, branch: Branch, cust_tin: str, correlation_id: uuid.UUID | None = None
    ) -> CustRes:
        req = CustReq(tin=branch.tin, bhfId=branch.bhf_id, custmTin=cust_tin)
        return await self._client.select_customer(branch.id, req, correlation_id=correlation_id)

    async def select_branches(
        self, branch: Branch, last_req_dt: str, correlation_id: uuid.UUID | None = None
    ) -> BhfRes:
        req = BhfReq(tin=branch.tin, bhfId=branch.bhf_id, lastReqDt=last_req_dt)
        return await self._client.select_branches(branch.id, req, correlation_id=correlation_id)

    async def save_branch_user(
        self,
        branch: Branch,
        user_id: str,
        user_nm: str,
        pwd: str,
        actor: str,
        adrs: str | None = None,
        cntc: str | None = None,
        auth_cd: str | None = None,
        remark: str | None = None,
        use_yn: str = "Y",
        correlation_id: uuid.UUID | None = None,
    ) -> BhfUserSaveRes:
        req = BhfUserSaveReq(
            tin=branch.tin,
            bhfId=branch.bhf_id,
            userId=user_id,
            userNm=user_nm,
            pwd=pwd,
            adrs=adrs,
            cntc=cntc,
            authCd=auth_cd,
            remark=remark,
            useYn=use_yn,
            regrId=actor,
            regrNm=actor,
            modrId=actor,
            modrNm=actor,
        )
        return await self._client.save_branch_user(branch.id, req, correlation_id=correlation_id)

    async def save_branch_insurance(
        self,
        branch: Branch,
        isrcc_cd: str,
        isrcc_nm: str,
        isrc_rt: Decimal,
        actor: str,
        use_yn: str = "Y",
        correlation_id: uuid.UUID | None = None,
    ) -> BhfInsuranceSaveRes:
        req = BhfInsuranceSaveReq(
            tin=branch.tin,
            bhfId=branch.bhf_id,
            isrccCd=isrcc_cd,
            isrccNm=isrcc_nm,
            isrcRt=isrc_rt,
            useYn=use_yn,
            regrId=actor,
            regrNm=actor,
            modrId=actor,
            modrNm=actor,
        )
        return await self._client.save_branch_insurance(
            branch.id, req, correlation_id=correlation_id
        )

    async def save_branch_customer(
        self,
        branch: Branch,
        cust_no: str,
        cust_nm: str,
        actor: str,
        cust_tin: str | None = None,
        adrs: str | None = None,
        tel_no: str | None = None,
        email: str | None = None,
        fax_no: str | None = None,
        use_yn: str = "Y",
        remark: str | None = None,
        correlation_id: uuid.UUID | None = None,
    ) -> BhfCustSaveRes:
        req = BhfCustSaveReq(
            tin=branch.tin,
            bhfId=branch.bhf_id,
            custNo=cust_no,
            custTin=cust_tin,
            custNm=cust_nm,
            adrs=adrs,
            telNo=tel_no,
            email=email,
            faxNo=fax_no,
            useYn=use_yn,
            remark=remark,
            regrId=actor,
            regrNm=actor,
            modrId=actor,
            modrNm=actor,
        )
        return await self._client.save_branch_customer(
            branch.id, req, correlation_id=correlation_id
        )

    async def select_items(
        self, branch: Branch, last_req_dt: str, correlation_id: uuid.UUID | None = None
    ) -> ItemRes:
        req = ItemReq(tin=branch.tin, bhfId=branch.bhf_id, lastReqDt=last_req_dt)
        return await self._client.select_items(branch.id, req, correlation_id=correlation_id)

    async def save_item(
        self, branch: Branch, item: ItemSaveReq, correlation_id: uuid.UUID | None = None
    ) -> ItemSaveRes:
        """See app/integrations/vscu/schemas/item.py — resultCd=="000" here does not prove
        real KRA accepted the item. Callers that need fiscal-grade confirmation should
        reconcile via select_items later, not trust this call's response alone.
        """
        return await self._client.save_item(branch.id, item, correlation_id=correlation_id)

    async def reconcile_item_by_cd(
        self, branch: Branch, item_cd: str, correlation_id: uuid.UUID | None = None
    ) -> Item | None:
        """General-purpose reconciliation for a specific item registration: look up item_cd
        via /items/selectItems — a verified plain proxy, real KRA's resultCd is trusted here
        (unlike /items/saveItems's confirmed 894/896 local-resend quirk — see save_item above
        and app/integrations/vscu/schemas/item.py's module docstring) — to confirm whether
        VSCU actually holds an item under that itemCd, rather than trusting a 201 from
        POST /v1/items alone. Mirrors vscu_stock_service.reconcile_by_sar_no and
        VSCUSalesService.reconcile_by_invoice_number: a side-channel diagnostic lookup, always
        searching from kra_sync_initial_last_req_dt rather than touching any poller watermark.
        """
        req = ItemReq(
            tin=branch.tin,
            bhfId=branch.bhf_id,
            lastReqDt=get_settings().kra_sync_initial_last_req_dt,
        )
        try:
            res = await self._client.select_items(branch.id, req, correlation_id=correlation_id)
        except VSCUError:
            logger.exception(
                "Item reconciliation lookup failed for branch=%s itemCd=%s", branch.id, item_cd
            )
            return None

        if res.data is None:
            return None
        return next((i for i in res.data.itemList if i.itemCd == item_cd), None)

    async def save_item_composition(
        self,
        branch: Branch,
        item_cd: str,
        cpst_item_cd: str,
        cpst_qty: Decimal,
        actor: str,
        correlation_id: uuid.UUID | None = None,
    ) -> ItemCompositionSaveRes:
        req = ItemCompositionSaveReq(
            tin=branch.tin,
            bhfId=branch.bhf_id,
            itemCd=item_cd,
            cpstItemCd=cpst_item_cd,
            cpstQty=cpst_qty,
            regrId=actor,
            regrNm=actor,
        )
        return await self._client.save_item_composition(
            branch.id, req, correlation_id=correlation_id
        )

    async def sync_notices(self, branch: Branch, correlation_id: uuid.UUID | None = None) -> None:
        """POST /notices/selectNotices, upserted into notice_cache — same table
        notice_service.refresh_notices (OSCU) writes to."""
        last_req_dt = await sync_framework.get_last_req_dt(
            self._db, branch.tin, branch.bhf_id, NOTICES_RESOURCE
        )
        req = NoticeReq(tin=branch.tin, bhfId=branch.bhf_id, lastReqDt=last_req_dt)
        res = await self._client.select_notices(branch.id, req, correlation_id=correlation_id)

        if res.data is not None:
            for notice in res.data.noticeList:
                stmt = pg_insert(NoticeCache).values(
                    branch_id=branch.id,
                    notice_no=str(notice.noticeNo),
                    title=notice.title,
                    cont=notice.cont,
                    dtl_url=notice.dtlUrl,
                    regr_nm=notice.regrNm,
                    reg_dt=notice.regDt,
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=[NoticeCache.branch_id, NoticeCache.notice_no],
                    set_={
                        "title": stmt.excluded.title,
                        "cont": stmt.excluded.cont,
                        "dtl_url": stmt.excluded.dtl_url,
                        "regr_nm": stmt.excluded.regr_nm,
                        "reg_dt": stmt.excluded.reg_dt,
                        "synced_at": func.now(),
                    },
                )
                await self._db.execute(stmt)

        if res.resultDt:
            await sync_framework.advance_last_req_dt(
                self._db, branch.tin, branch.bhf_id, NOTICES_RESOURCE, res.resultDt
            )

    async def sync_import_items(
        self, branch: Branch, correlation_id: uuid.UUID | None = None
    ) -> None:
        """POST /imports/selectImportItems, upserted into import_item_cache — same table
        import_item_service.refresh_import_items (OSCU) writes to."""
        last_req_dt = await sync_framework.get_last_req_dt(
            self._db, branch.tin, branch.bhf_id, IMPORT_ITEMS_RESOURCE
        )
        req = ImportItemReq(tin=branch.tin, bhfId=branch.bhf_id, lastReqDt=last_req_dt)
        res = await self._client.select_import_items(branch.id, req, correlation_id=correlation_id)

        if res.data is not None:
            for entry in res.data.itemList:
                stmt = pg_insert(ImportItemCache).values(
                    branch_id=branch.id,
                    task_cd=entry.taskCd,
                    dcl_de=entry.dclDe,
                    item_seq=entry.itemSeq,
                    dcl_no=entry.dclNo,
                    hs_cd=entry.hsCd,
                    item_nm=entry.itemNm,
                    orgn_nat_cd=entry.orgnNatCd,
                    raw_data=entry.model_dump(mode="json"),
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=[
                        ImportItemCache.branch_id,
                        ImportItemCache.task_cd,
                        ImportItemCache.dcl_de,
                        ImportItemCache.item_seq,
                    ],
                    set_={
                        "dcl_no": stmt.excluded.dcl_no,
                        "hs_cd": stmt.excluded.hs_cd,
                        "item_nm": stmt.excluded.item_nm,
                        "orgn_nat_cd": stmt.excluded.orgn_nat_cd,
                        "raw_data": stmt.excluded.raw_data,
                        "synced_at": func.now(),
                    },
                )
                await self._db.execute(stmt)

        if res.resultDt:
            await sync_framework.advance_last_req_dt(
                self._db, branch.tin, branch.bhf_id, IMPORT_ITEMS_RESOURCE, res.resultDt
            )

    async def update_import_item(
        self, branch: Branch, req: ImportItemUpdateReq, correlation_id: uuid.UUID | None = None
    ) -> ImportItemUpdateRes:
        return await self._client.update_import_item(branch.id, req, correlation_id=correlation_id)
