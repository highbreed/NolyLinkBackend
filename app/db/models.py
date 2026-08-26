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
    status: Mapped[str] = mapped_column(String, default="ACTIVE", server_default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    items: Mapped[list["Item"]] = relationship(back_populates="branch")


class Client(Base):
    """An inbound integration client (SAP-PROD, POS-VENDOR-X, ...) — spec §15."""

    __tablename__ = "clients"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String)
    api_key_hash: Mapped[str] = mapped_column(String)
    allowed_branch_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


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
    kra_synced_at: Mapped[datetime | None] = mapped_column()

    branch: Mapped["Branch"] = relationship(back_populates="items")


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
    """Per-(tin, bhfId, resource) lastReqDt watermark for incremental polling (spec §11)."""

    __tablename__ = "kra_sync_state"

    tin: Mapped[str] = mapped_column(CHAR(11), primary_key=True)
    bhf_id: Mapped[str] = mapped_column(CHAR(2), primary_key=True)
    resource: Mapped[str] = mapped_column(String, primary_key=True)
    last_req_dt: Mapped[str] = mapped_column(String)


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
