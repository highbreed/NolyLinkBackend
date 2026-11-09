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

    device_type/kra_base_url_override originally assumed OSCU and VSCU devices shared one
    request/response contract, differing only by host — disproven 2026-09-01
    (docs/kra_specs/README.md): VSCU is a genuinely different local integration, handled by
    app/integrations/vscu/ (see VscuDeviceState below), not by kra_base_url_for(). These two
    columns remain load-bearing for OSCU-only routing (kra_base_url_override still lets an
    OSCU branch pin a custom host); device_type == "VSCU" should be treated as "route through
    app/integrations/vscu/", not as an input to kra_base_url_for().

    cmc_key is None for a VSCU branch (2026-09-02, follow-up to the correction above): the
    decompiled vscu-v2.0.21.jar's confirmed endpoint list has no KRA-hosted registration call
    at all for VSCU (no /selectInitOsdcInfo-equivalent) — the local VSCU app's own
    /initializer/selectInitInfo call handles KRA registration internally and strips cmcKey
    before ever responding to us (see VscuDeviceState's docstring). So
    app/services/cmc_key_manager.py never calls real KRA to create a VSCU Branch row; it's
    created locally from admin-supplied (branch_code, tin, bhfId) with no cmcKey to store.
    """

    __tablename__ = "branches"
    __table_args__ = (UniqueConstraint("tin", "bhf_id", name="uq_branches_tin_bhf_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    branch_code: Mapped[str] = mapped_column(String, unique=True, index=True)
    tin: Mapped[str] = mapped_column(CHAR(11))
    bhf_id: Mapped[str] = mapped_column(CHAR(2))
    cmc_key: Mapped[str | None] = mapped_column(EncryptedString)
    dvc_id: Mapped[str | None] = mapped_column(String)
    sdc_id: Mapped[str | None] = mapped_column(String)
    mrc_no: Mapped[str | None] = mapped_column(String)
    device_type: Mapped[str] = mapped_column(String, default="OSCU", server_default="OSCU")
    kra_base_url_override: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="ACTIVE", server_default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    items: Mapped[list["Item"]] = relationship(back_populates="branch")


class AdminUser(Base):
    """A human account for the backend admin portal — sits alongside the static
    X-Admin-Api-Key (app/core/admin_auth.py), not in place of it: either credential grants the
    same admin access, so existing scripts/CI using the key keep working. See
    app/services/admin_user_auth.py for password hashing (bcrypt, not sha256 — a human
    password is low-entropy and needs slow, salted hashing, unlike Client.client_secret_hash's
    generated high-entropy secret) and JWT issuance.
    """

    __tablename__ = "admin_users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String)
    full_name: Mapped[str | None] = mapped_column(String)
    is_active: Mapped[bool] = mapped_column(default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column()


class Customer(Base):
    """The paying business we integrate this system for (2026-09-01) — e.g. "Naivas
    Supermarket". Distinct from Client (below): a Customer is the tenant that owns branch
    access and billing; a Client is one OAuth2 credential/connector integrating on that
    Customer's behalf (their SAP instance, their POS vendor's middleware, ...). One Customer
    can have several Clients (e.g. a prod connector and a staging one, or two different
    systems both submitting on their behalf) — see Client.customer_id.

    allowed_branch_ids lived on Client before this; it moved here because branch access is a
    property of the paying business, not of any one connector — every Client under a Customer
    inherits the same branches and the same billing status (Subscription, below).
    """

    __tablename__ = "customers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String)
    allowed_branch_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    clients: Mapped[list["Client"]] = relationship(back_populates="customer")


class CustomerCodeAllowlist(Base):
    """Per-Customer restriction on which KRA reference codes a Client under it may submit
    (2026-09-18) — an EAV-style join table, not per-classification array columns on Customer,
    so that "no rows for (customer_id, classification)" is the one unambiguous unrestricted
    state (every pre-existing Customer stays fully unrestricted with no backfill) and adding a
    6th restrictable classification later needs no schema change.

    classification is a code_cache_service.CLASSIFICATION_SLUGS key (tax-type/packaging-unit/
    quantity-unit/nation) or the literal pseudo-slug "item-classification" (item classes have
    no cd_cls of their own — see KraItemClass). code is the resolved KRA code value (e.g. "B",
    "NT"), not a friendly name — see app/services/code_allowlist_service.py.
    """

    __tablename__ = "customer_code_allowlist"

    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id"), primary_key=True
    )
    classification: Mapped[str] = mapped_column(String, primary_key=True)
    code: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Client(Base):
    """One OAuth2 integration credential/connector (SAP-PROD, POS-VENDOR-X, ...) — spec §15.
    Belongs to exactly one Customer (the paying business it integrates on behalf of); see
    Customer's docstring for why branch access and billing live there, not here.

    id doubles as the OAuth2 client_id sent to POST /oauth/token (spec §15's
    client-credentials grant); client_secret_hash is that flow's client_secret, hashed.

    revoked_at (2026-10-15) is a soft-revoke, not a delete: IdempotencyKey.client_id is a real
    FK to this table (spec §13's dedup record), and a revoked connector may have genuine fiscal
    history behind it — hard-deleting the row would either violate that FK or force deleting
    idempotency records that exist specifically to prevent a duplicate KRA submission. A
    revoked client's existing JWTs are rejected by client_auth.get_current_client the moment
    this is set, same as an unknown/invalid token (401) — not distinguished in the response, so
    a caller can't tell "revoked" from "never existed".
    """

    __tablename__ = "clients"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id"), index=True
    )
    name: Mapped[str] = mapped_column(String)
    client_secret_hash: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column()

    customer: Mapped["Customer"] = relationship(back_populates="clients")


class Subscription(Base):
    """Per-customer billing subscription — this gateway's own tenant billing, entirely outside
    the KRA spec. Internal tracking only: an admin (app/api/v1/endpoints/billing.py) sets
    current_period_end/status directly; there is no payment processor integration (same
    "don't fabricate an integration without real credentials" policy this build followed for
    KRA sandbox access all along).

    Keyed by customer_id (2026-09-01, moved from client_id): billing is a property of the
    paying business, not of any one of its connectors — see Customer's docstring.

    status is only ACTIVE/SUSPENDED/CANCELLED — "lapsed but within grace period" is computed
    at request time from current_period_end + grace_period_days
    (app/services/billing_service.py), not tracked as a separate status, so nothing needs to
    proactively flip it at the moment a grace period ends.
    """

    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id"), unique=True, index=True
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
    """One billing-period invoice for a customer. Internal record only — see Subscription's
    docstring on payment-processor scope and on why this is keyed by customer_id, not
    client_id.
    """

    __tablename__ = "invoices"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id"), index=True
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


class KraCodeClassification(Base):
    """The real name KRA gives each cd_cls (e.g. "Tax Type" for cd_cls="04") — spec §6.2's
    CodeClass.cdClsNm/cdClsDesc (OSCU) / CodeClassLVO.cdClsNm/cdClsDesc (VSCU), which is a
    property of the classification itself, not of any one code within it (see KraCodeList
    below), so it's a separate small table rather than a column repeated on every code row.

    Added 2026-09-18 after seeding real data revealed several cd_cls values (e.g. "36" = bank
    list, "45" = KRA station list) that code_cache_service.CLASSIFICATION_SLUGS — built from
    the spec's own partial table — has no friendly slug for. KRA was supplying their real names
    all along via cdClsNm; earlier syncs just discarded it. This is that name, not a guess.
    """

    __tablename__ = "kra_code_classifications"

    cd_cls: Mapped[str] = mapped_column(String, primary_key=True)
    cd_cls_nm: Mapped[str | None] = mapped_column(String)
    cd_cls_desc: Mapped[str | None] = mapped_column(String)
    refreshed_at: Mapped[datetime] = mapped_column(server_default=func.now())


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


class KraItemClass(Base):
    """Cached item-classification rows from /selectItemClsList (OSCU, spec §6.3) and VSCU's
    /itemClass/selectItemsClass — see app/services/item_class_cache_service.py. Unlike
    KraCodeList this has no cd_cls: item classes aren't one of the numbered classifications
    /selectCodeList serves, they're their own KRA resource, so this is keyed directly by
    item_cls_cd instead of a (cd_cls, cd) composite.
    """

    __tablename__ = "kra_item_classes"

    item_cls_cd: Mapped[str] = mapped_column(String, primary_key=True)
    item_cls_nm: Mapped[str | None] = mapped_column(String)
    item_cls_lvl: Mapped[str | None] = mapped_column(String)
    tax_ty_cd: Mapped[str | None] = mapped_column(String)
    mjr_tg_yn: Mapped[str | None] = mapped_column(CHAR(1))
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


class VscuDeviceState(Base):
    """Operational state AND connection config for a branch's local VSCU device
    (app/integrations/vscu/), one row per branch. This is a multi-tenant system — every client
    picks OSCU or VSCU independently per branch (Branch.device_type), and unlike OSCU (one KRA
    host shared by everyone), a VSCU device runs on *that specific client's own premises/
    network*, so base_url is per-branch config here, not a global Settings value (mirrors
    Branch.kra_base_url_override's per-branch precedent for OSCU). Settings.vscu_base_url is
    only a fallback default for single-tenant/dev use, never assumed correct for a real client.

    Deliberately excludes cmcKey/signKey/intrlKey — those never leave the local VSCU app
    (confirmed by decompiling InitializeVsdcExcute.selectInitInfo, which strips them from its
    own response before we ever see it). VSCU's local H2 store remains the authoritative
    sequence owner; last_known_* here is a cache for our own visibility/audit, never a source
    of truth to submit against.
    """

    __tablename__ = "vscu_device_state"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id"), unique=True, index=True
    )
    # This branch's local VSCU app, e.g. "http://192.168.1.10:8088" — required; every VSCU
    # branch lives on a different client's network, so there is no sane shared default.
    base_url: Mapped[str] = mapped_column(String)
    # None falls back to Settings.vscu_connect/read_timeout_seconds. Per-branch override exists
    # because a client's own network/VSCU hardware may need different timeouts than the norm.
    connect_timeout_seconds: Mapped[float | None] = mapped_column()
    read_timeout_seconds: Mapped[float | None] = mapped_column()
    dvc_srl_no: Mapped[str | None] = mapped_column(String)
    sdc_id: Mapped[str | None] = mapped_column(String)
    mrc_no: Mapped[str | None] = mapped_column(String)
    dvc_id: Mapped[str | None] = mapped_column(String)
    vat_ty_cd: Mapped[str | None] = mapped_column(String)
    taxpr_nm: Mapped[str | None] = mapped_column(String)
    bhf_nm: Mapped[str | None] = mapped_column(String)
    initialized_at: Mapped[datetime | None] = mapped_column()
    last_sequence_sync_at: Mapped[datetime | None] = mapped_column()
    last_known_invc_no: Mapped[int | None] = mapped_column(BigInteger)
    last_known_sale_rcpt_no: Mapped[int | None] = mapped_column(BigInteger)
    last_successful_call_at: Mapped[datetime | None] = mapped_column()
    last_error: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class VscuCallLog(Base):
    """Audit log of every local-VSCU-app call — mirrors KraCallLog's shape/intent but is kept
    separate since VSCU is a distinct downstream system with its own endpoint/result-code
    space (app/integrations/vscu/constants.py), not a KRA host. Never logs the raw payload
    (may carry receipt signature/internal data) — only its hash, per the same rule KraCallLog
    follows for cmcKey.
    """

    __tablename__ = "vscu_call_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id"), index=True
    )
    correlation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), default=uuid.uuid4)
    endpoint: Mapped[str] = mapped_column(String)
    request_hash: Mapped[str] = mapped_column(String)
    result_cd: Mapped[str | None] = mapped_column(String)
    result_msg: Mapped[str | None] = mapped_column(String)
    result_dt: Mapped[str | None] = mapped_column(String)
    http_status: Mapped[int | None] = mapped_column()
    latency_ms: Mapped[int | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SalesInvoice(Base):
    """Fiscal receipt record — rcpt_sign/intrl_data/etc. must be immutable once written.
    See spec §19.

    transaction_kind ("SALE" or "CREDIT_NOTE", 2026-09-01) exists because a credit note is a
    genuinely separate fiscal transaction referencing an original sale via kra_invc_no's
    counterpart (orgInvcNo on the wire), not a mutation of it — see
    app/schemas/client/credit_note_in.py's module docstring. It's tracked under its own
    idempotency endpoint namespace ("/v1/sales-invoices/credit-notes"), but without this
    column a client reusing the same external_ref string for an unrelated sale and credit note
    on the same branch would still collide on this table's own uniqueness, even though the
    idempotency layer treats them as different operations.
    """

    __tablename__ = "sales_invoices"
    __table_args__ = (
        UniqueConstraint(
            "branch_id",
            "external_ref",
            "transaction_kind",
            name="uq_sales_invoices_branch_id_external_ref_transaction_kind",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.id"), index=True
    )
    external_ref: Mapped[str] = mapped_column(String)
    transaction_kind: Mapped[str] = mapped_column(String, default="SALE", server_default="SALE")
    # Set only for transaction_kind="CREDIT_NOTE" — the original sale's own external_ref, for
    # audit trail (e.g. "list every credit note issued against invoice X"). Deliberately a
    # plain string, not a FK to this same table's id: it's set from the client-supplied
    # original_invoice_ref before the original row's id is looked up at all in some paths, and
    # a string keeps this column meaningful even if the original row is ever purged.
    original_ref: Mapped[str | None] = mapped_column(String)
    kra_invc_no: Mapped[int | None] = mapped_column(BigInteger)
    kra_cur_rcpt_no: Mapped[int | None] = mapped_column(BigInteger)
    kra_tot_rcpt_no: Mapped[int | None] = mapped_column(BigInteger)
    kra_rcpt_sign: Mapped[str | None] = mapped_column(String)
    kra_intrl_data: Mapped[str | None] = mapped_column(String)
    kra_sdc_datetime: Mapped[str | None] = mapped_column(String)
    # The receipt's "CU ID" (TIS-OSCU-VSCU-v2.0 spec §6.23.3, e.g. "KRACU0300004030") —
    # required on the printed receipt alongside the "CU Invoice Number" (§6.23.4: CU ID + "/" +
    # kra_cur_rcpt_no), which app/api/v1/endpoints/sales.py computes from this rather than
    # storing separately. VSCU's /trnsSales/saveSales response carries sdcId directly (captured
    # here at write time); OSCU's doesn't, so the OSCU write path uses the branch's own
    # Branch.sdc_id (set once at device init, not per-transaction) instead.
    kra_sdc_id: Mapped[str | None] = mapped_column(String)
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


class ClientCallLog(Base):
    """Persisted record of every inbound HTTP call this gateway received and what it answered
    — not to be confused with KraCallLog/VscuCallLog (our calls TO KRA/VSCU). Written by
    AuditLogMiddleware (app/core/audit.py) alongside its existing stdout logging, so "what did
    a client actually send us, and what did we send back" is queryable from the admin portal
    instead of living only in transient log output. Bodies are redacted the same way the
    stdout audit log already is (app.core.audit._redact/REDACTED_KEYS).

    Cleaned up after client_call_log_retention_days (app/workers/cleanup.py) — raw request/
    response bodies are fiscal-PII-adjacent even after redaction, so this is not kept forever.
    """

    __tablename__ = "client_call_log"
    __table_args__ = (
        Index("ix_client_call_log_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String, index=True)
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("clients.id"), index=True
    )
    method: Mapped[str] = mapped_column(String)
    path: Mapped[str] = mapped_column(String)
    status_code: Mapped[int] = mapped_column()
    duration_ms: Mapped[int] = mapped_column()
    request_body: Mapped[dict[str, object] | list[object] | str | None] = mapped_column(JSONB)
    response_body: Mapped[dict[str, object] | list[object] | str | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AdminAuditLog(Base):
    """A record of every admin action ("admin X suspended customer Y") — distinct from both
    ClientCallLog (what SAP/ERP clients sent us) and the stdout-only logger.info() calls
    scattered across admin.py/billing.py/customers.py/device.py, which this supplements, not
    replaces. Written by app/services/admin_audit.py::record_admin_action, called inside the
    same transaction as the action it records (before that endpoint's own commit), so it's
    never out of sync with what actually happened.

    admin_user_email is really AdminUser.username, not a verified email — AdminUser has no
    email column (see its own docstring) — kept under this name only because the frontend's
    AuditLogEntry TS type already committed to it; None means the static X-Admin-Api-Key
    break-glass credential was used instead of a real admin login.
    """

    __tablename__ = "admin_audit_log"
    __table_args__ = (Index("ix_admin_audit_log_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    admin_user_email: Mapped[str | None] = mapped_column(String)
    action: Mapped[str] = mapped_column(String)
    target_type: Mapped[str] = mapped_column(String, index=True)
    target_id: Mapped[str] = mapped_column(String, index=True)
    before_state: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    after_state: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(String)
    ip_address: Mapped[str | None] = mapped_column(String)
    request_id: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AlertRule(Base):
    """A threshold rule over one of metrics_service.KNOWN_METRICS, evaluated every 5 minutes
    by app/workers/alert_evaluator.py. scope_customer_id/scope_branch_code are two columns,
    not the single `scope` text column admin_portal_design.md's §7 SQL proposes — the
    frontend's AlertRule/AlertRuleInput TS types already committed to the split-column
    version, so matching that stale doc would be pure wasted translation now.
    """

    __tablename__ = "alert_rules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String)
    metric: Mapped[str] = mapped_column(String)
    scope_customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id")
    )
    scope_branch_code: Mapped[str | None] = mapped_column(String)
    condition: Mapped[str] = mapped_column(String)
    threshold: Mapped[Decimal] = mapped_column(Numeric)
    channel: Mapped[str] = mapped_column(String)
    channel_target: Mapped[str] = mapped_column(String)
    enabled: Mapped[bool] = mapped_column(default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class NotificationSent(Base):
    """One row per alert-rule firing, written by app/workers/alert_evaluator.py. There is no
    resolved_at tracking yet (always None) — that needs knowing when a firing rule's metric
    drops back below threshold, which this first pass doesn't attempt; a fired notification is
    a point-in-time event here, not an ongoing incident with a lifecycle.

    delivered=False + delivery_error is the normal, expected state for channel="email" until
    real SMTP settings exist (no credentials were available when this was built) — not a bug.
    """

    __tablename__ = "notifications_sent"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    alert_rule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("alert_rules.id"), index=True
    )
    fired_at: Mapped[datetime] = mapped_column(server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column()
    metric_value: Mapped[Decimal] = mapped_column(Numeric)
    summary: Mapped[str] = mapped_column(String)
    delivered: Mapped[bool] = mapped_column(default=False, server_default="false")
    delivery_error: Mapped[str | None] = mapped_column(String)
