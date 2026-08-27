"""SQLAlchemy models: branches, clients, items, kra_code_lists, kra_sync_state,
idempotency_keys, outbox, kra_call_log, sales_invoices (spec §17).
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.db.encrypted_types import EncryptedString


class Base(DeclarativeBase):
    """Shared declarative base; Alembic's env.py targets Base.metadata for autogenerate."""

    type_annotation_map = {datetime: DateTime(timezone=True)}


class Branch(Base):
    """A KRA-registered (tin, bhfId) device. cmc_key is encrypted at rest via EncryptedString
    (spec §12.2) — reads/writes see the plaintext key; only the DB column holds ciphertext.

    device_type/kra_base_url_override let one gateway instance serve OSCU and VSCU devices
    side by side — same request/response contract either way, only the host differs.
    kra_base_url_override is null for the common case (falls back to the global
    Settings.kra_base_url for that branch's device_type); set per-branch at init time when a
    branch needs to hit a non-default host.
    """

    __tablename__ = "branches"
    __table_args__ = (UniqueConstraint("tin", "bhf_id", name="uq_branches_tin_bhf_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    branch_code: Mapped[str] = mapped_column(String, unique=True, index=True)
    tin: Mapped[str] = mapped_column(CHAR(11))
    bhf_id: Mapped[str] = mapped_column(CHAR(2))
    cmc_key: Mapped[str] = mapped_column(EncryptedString)
    dvc_id: Mapped[str | None] = mapped_column(String)
    sdc_id: Mapped[str | None] = mapped_column(String)
    mrc_no: Mapped[str | None] = mapped_column(String)
    device_type: Mapped[str] = mapped_column(String, default="OSCU", server_default="OSCU")
    kra_base_url_override: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="ACTIVE", server_default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    items: Mapped[list["Item"]] = relationship(back_populates="branch")


class Client(Base):
    """An inbound integration client (SAP-PROD, POS-VENDOR-X, ...) — spec §15.

    id doubles as the OAuth2 client_id sent to POST /oauth/token (spec §15's
    client-credentials grant); client_secret_hash is that flow's client_secret, hashed.
    """

    __tablename__ = "clients"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String)
    client_secret_hash: Mapped[str] = mapped_column(String)
    allowed_branch_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Subscription(Base):
    """Per-client billing subscription — this gateway's own tenant billing, entirely outside
    the KRA spec. Internal tracking only: an admin (app/api/v1/endpoints/billing.py) sets
    current_period_end/status directly; there is no payment processor integration (same
    "don't fabricate an integration without real credentials" policy this build followed for
    KRA sandbox access all along).

    status is only ACTIVE/SUSPENDED/CANCELLED — "lapsed but within grace period" is computed
    at request time from current_period_end + grace_period_days
    (app/services/billing_service.py), not tracked as a separate status, so nothing needs to
    proactively flip it at the moment a grace period ends.
    """

    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id"), unique=True, index=True
    )
    plan_name: Mapped[str] = mapped_column(String, default="STANDARD", server_default="STANDARD")
    status: Mapped[str] = mapped_column(String, default="ACTIVE", server_default="ACTIVE")
    current_period_end: Mapped[datetime] = mapped_column()
    # None -> Settings.billing_grace_period_days applies instead.
    grace_period_days: Mapped[int | None] = mapped_column()
    # Set only by an explicit suspend action (e.g. fraud, chargeback) — independent of
    # current_period_end, so renewing the period alone can never silently lift a suspension.
    suspended_at: Mapped[datetime | None] = mapped_column()
    suspended_reason: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class Invoice(Base):
    """One billing-period invoice for a client. Internal record only — see Subscription's
    docstring on payment-processor scope.
    """

    __tablename__ = "invoices"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id"), index=True
    )
    period_start: Mapped[datetime] = mapped_column()
    period_end: Mapped[datetime] = mapped_column()
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String, default="KES", server_default="KES")
    status: Mapped[str] = mapped_column(String, default="ISSUED", server_default="ISSUED")
    issued_at: Mapped[datetime] = mapped_column(server_default=func.now())
    paid_at: Mapped[datetime | None] = mapped_column()
    notes: Mapped[str | None] = mapped_column(String)


class Item(Base):
    """Item master, keyed by client-facing sku and KRA-format item_cd (spec §8)."""

    __tablename__ = "items"
    __table_args__ = (UniqueConstraint("branch_id", "sku", name="uq_items_branch_id_sku"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id"), index=True
    )
    sku: Mapped[str] = mapped_column(String)
    item_cd: Mapped[str] = mapped_column(String, index=True)
    item_cls_cd: Mapped[str] = mapped_column(String)
    item_nm: Mapped[str] = mapped_column(String)
    tax_ty_cd: Mapped[str] = mapped_column(CHAR(1))
    dft_prc: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    # Not in spec §17's literal DDL, but needed to build sales line items (spec §6.6's
    # TrnsSalesSaveWrItem requires pkgUnitCd/qtyUnitCd, and the client sales DTO in spec §9.2
    # doesn't resupply them per line) — captured once at item registration (Phase 6) instead.
    pkg_unit_cd: Mapped[str] = mapped_column(String)
    qty_unit_cd: Mapped[str] = mapped_column(String)
    kra_synced_at: Mapped[datetime | None] = mapped_column()

    branch: Mapped["Branch"] = relationship(back_populates="items")


class ItemCodeSequence(Base):
    """Backs the itemCd auto-generator (spec §8): one row per (tin, bhfId, orgnNatCd,
    itemTyCd, pkgUnitCd, qtyUnitCd) composite, since a single Postgres SEQUENCE object can't
    be dynamically keyed. next_seq is incremented atomically via an upsert in
    app/services/item_code_generator.py.
    """

    __tablename__ = "item_code_sequences"

    tin: Mapped[str] = mapped_column(CHAR(11), primary_key=True)
    bhf_id: Mapped[str] = mapped_column(CHAR(2), primary_key=True)
    orgn_nat_cd: Mapped[str] = mapped_column(String, primary_key=True)
    item_ty_cd: Mapped[str] = mapped_column(String, primary_key=True)
    pkg_unit_cd: Mapped[str] = mapped_column(String, primary_key=True)
    qty_unit_cd: Mapped[str] = mapped_column(String, primary_key=True)
    next_seq: Mapped[int] = mapped_column(default=1, server_default="1")


class SalesInvoiceSequence(Base):
    """Backs invcNo (spec §6.6's required header field) — not given to us by KRA, so the
    gateway assigns its own per-(tin, bhfId) sequence, same atomic-upsert pattern as
    ItemCodeSequence.
    """

    __tablename__ = "sales_invoice_sequences"

    tin: Mapped[str] = mapped_column(CHAR(11), primary_key=True)
    bhf_id: Mapped[str] = mapped_column(CHAR(2), primary_key=True)
    next_seq: Mapped[int] = mapped_column(default=1, server_default="1")


class KraCodeList(Base):
    """Cached KRA code table rows, refreshed via /selectCodeList (spec §7, §11)."""

    __tablename__ = "kra_code_lists"

    cd_cls: Mapped[str] = mapped_column(String, primary_key=True)
    cd: Mapped[str] = mapped_column(String, primary_key=True)
    cd_nm: Mapped[str | None] = mapped_column(String)
    cd_desc: Mapped[str | None] = mapped_column(String)
    srt_ord: Mapped[int | None] = mapped_column()
    use_yn: Mapped[str | None] = mapped_column(CHAR(1))
    refreshed_at: Mapped[datetime] = mapped_column(server_default=func.now())


class KraSyncState(Base):
    """Per-(tin, bhfId, resource) lastReqDt watermark for incremental polling (spec §11).

    updated_at is our own wall-clock time of the last successful sync, not spec §17's literal
    DDL — added for Phase 10's "/v1/health includes last successful poll timestamp per
    resource" and sync-lag metric, since last_req_dt is KRA's own timestamp (a resultDt
    string, spec §11) and isn't reliably comparable to wall-clock "how stale is this".
    """

    __tablename__ = "kra_sync_state"

    tin: Mapped[str] = mapped_column(CHAR(11), primary_key=True)
    bhf_id: Mapped[str] = mapped_column(CHAR(2), primary_key=True)
    resource: Mapped[str] = mapped_column(String, primary_key=True)
    last_req_dt: Mapped[str] = mapped_column(String)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class IdempotencyKey(Base):
    """Dedupes mutating client requests by (client_id, external_ref, endpoint) — spec §13."""

    __tablename__ = "idempotency_keys"

    client_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id"), primary_key=True
    )
    external_ref: Mapped[str] = mapped_column(String, primary_key=True)
    endpoint: Mapped[str] = mapped_column(String, primary_key=True)
    kra_response: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Outbox(Base):
    """Outbox pattern row for async KRA dispatch with retry/backoff (spec §14)."""

    __tablename__ = "outbox"
    __table_args__ = (Index("ix_outbox_status", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    endpoint: Mapped[str] = mapped_column(String)
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id"), index=True
    )
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String, default="PENDING", server_default="PENDING")
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class KraCallLog(Base):
    """Audit log of every KRA call: endpoint, hashed request, resultCd/Msg/Dt (spec §10)."""

    __tablename__ = "kra_call_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id"), index=True
    )
    endpoint: Mapped[str] = mapped_column(String)
    request_hash: Mapped[str] = mapped_column(String)
    result_cd: Mapped[str | None] = mapped_column(String)
    result_msg: Mapped[str | None] = mapped_column(String)
    result_dt: Mapped[str | None] = mapped_column(String)
    retry_count: Mapped[int] = mapped_column(default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SalesInvoice(Base):
    """Fiscal receipt record — rcpt_sign/intrl_data/etc. must be immutable once written.
    See spec §19.
    """

    __tablename__ = "sales_invoices"
    __table_args__ = (
        UniqueConstraint(
            "branch_id", "external_ref", name="uq_sales_invoices_branch_id_external_ref"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id"), index=True
    )
    external_ref: Mapped[str] = mapped_column(String)
    kra_invc_no: Mapped[int | None] = mapped_column(BigInteger)
    kra_cur_rcpt_no: Mapped[int | None] = mapped_column(BigInteger)
    kra_tot_rcpt_no: Mapped[int | None] = mapped_column(BigInteger)
    kra_rcpt_sign: Mapped[str | None] = mapped_column(String)
    kra_intrl_data: Mapped[str | None] = mapped_column(String)
    kra_sdc_datetime: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="PENDING", server_default="PENDING")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


# --- Phase 8: purchases, stock, imports, notices ---------------------------------------
#
# None of these tables are in spec §17's literal DDL (only branches/clients/items/
# kra_code_lists/kra_sync_state/idempotency_keys/outbox/kra_call_log/sales_invoices are), but
# the Phase 8 endpoints explicitly need somewhere to cache "Get" data and record "Send"
# submissions — the same situation Phase 6 hit with item_code_sequences.


class ImportItemCache(Base):
    """Cache of /selectImportItemList entries (spec §6.5), plus our own decision status once
    POST /v1/imports/{taskCd}/decision has acted on a row.
    """

    __tablename__ = "import_item_cache"
    __table_args__ = (
        UniqueConstraint(
            "branch_id", "task_cd", "dcl_de", "item_seq", name="uq_import_item_cache_natural_key"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id"), index=True
    )
    task_cd: Mapped[str] = mapped_column(String, index=True)
    dcl_de: Mapped[str] = mapped_column(String)
    item_seq: Mapped[int] = mapped_column()
    dcl_no: Mapped[str] = mapped_column(String)
    hs_cd: Mapped[str] = mapped_column(String)
    item_nm: Mapped[str] = mapped_column(String)
    orgn_nat_cd: Mapped[str] = mapped_column(String)
    item_cls_cd: Mapped[str | None] = mapped_column(String)
    item_cd: Mapped[str | None] = mapped_column(String)
    impt_item_stts_cd: Mapped[str] = mapped_column(String, default="1", server_default="1")
    raw_data: Mapped[dict[str, object]] = mapped_column(JSONB)
    synced_at: Mapped[datetime] = mapped_column(server_default=func.now())


class PurchaseSalesCache(Base):
    """Cache of /selectTrnsPurchaseSalesList entries (spec §6.7) — supplier-declared sales
    not yet matched to one of our own purchase confirmations.

    raw_data holds the full PurchaseSalesEntry (header fields incl. every taxblAmt/taxRt/
    taxAmt A-E bucket, plus item_list) verbatim as returned by KRA. Only a few fields are
    pulled out as real columns for filtering/display; everything else is reconstructed from
    raw_data when confirming a purchase (app/services/purchase_service.py), so the buckets
    sent back to KRA are exactly what KRA itself declared — never recomputed or guessed.
    """

    __tablename__ = "purchase_sales_cache"
    __table_args__ = (
        UniqueConstraint(
            "branch_id", "spplr_tin", "spplr_invc_no", name="uq_purchase_sales_cache_natural_key"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id"), index=True
    )
    spplr_tin: Mapped[str] = mapped_column(CHAR(11))
    spplr_invc_no: Mapped[str] = mapped_column(String)
    tot_amt: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    raw_data: Mapped[dict[str, object]] = mapped_column(JSONB)
    confirmed: Mapped[bool] = mapped_column(default=False, server_default="false")
    synced_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Purchase(Base):
    """Business record for a purchase we submit via /insertTrnsPurchase (spec §6.7),
    dispatched asynchronously through the outbox (spec §14).
    """

    __tablename__ = "purchases"
    __table_args__ = (
        UniqueConstraint("branch_id", "external_ref", name="uq_purchases_branch_id_external_ref"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id"), index=True
    )
    external_ref: Mapped[str] = mapped_column(String)
    outbox_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("outbox.id"))
    status: Mapped[str] = mapped_column(String, default="PENDING", server_default="PENDING")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class StockMoveCache(Base):
    """Cache of /selectStockMoveList entries. spec §6.8/§20 gives no field list at all for
    this endpoint's response (only "inter-branch movement history") — raw_data is a
    permissive JSONB blob rather than invented columns, same pattern as
    app/schemas/kra/stock.py's StockMoveEntry.
    """

    __tablename__ = "stock_move_cache"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id"), index=True
    )
    raw_data: Mapped[dict[str, object]] = mapped_column(JSONB)
    synced_at: Mapped[datetime] = mapped_column(server_default=func.now())


class StockIOSubmission(Base):
    """Business record for a stock in/out event submitted via /insertStockIO (spec §6.8),
    dispatched asynchronously through the outbox (spec §14).
    """

    __tablename__ = "stock_io_submissions"
    __table_args__ = (
        UniqueConstraint(
            "branch_id", "external_ref", name="uq_stock_io_submissions_branch_id_external_ref"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id"), index=True
    )
    external_ref: Mapped[str] = mapped_column(String)
    outbox_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("outbox.id"))
    status: Mapped[str] = mapped_column(String, default="PENDING", server_default="PENDING")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class NoticeCache(Base):
    """Cache of /selectNoticeList entries (spec §6.2)."""

    __tablename__ = "notice_cache"
    __table_args__ = (
        UniqueConstraint("branch_id", "notice_no", name="uq_notice_cache_natural_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id"), index=True
    )
    notice_no: Mapped[str] = mapped_column(String)
    title: Mapped[str] = mapped_column(String)
    cont: Mapped[str] = mapped_column(String)
    dtl_url: Mapped[str | None] = mapped_column(String)
    regr_nm: Mapped[str | None] = mapped_column(String)
    reg_dt: Mapped[str | None] = mapped_column(String)
    synced_at: Mapped[datetime] = mapped_column(server_default=func.now())
