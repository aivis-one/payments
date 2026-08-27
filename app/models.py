"""ORM models: ``invoices`` and ``invoice_txid_attempts`` (TOR section 4).

Status and result-code columns are plain ``String``, not ``SAEnum`` and without
a ``CHECK``: that is the project convention for status/type columns, and it is
also what keeps the network list open (TOR section 12).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Invoice(Base):
    """One top-up request: amount, address snapshot, lifecycle status.

    Worker fields (``confirmations_seen``, ``last_checked_at``,
    ``next_check_at``) are absent on purpose -- they belong to the background
    worker that does not exist yet, and columns nobody reads or writes would be
    an extension of this delivery's scope. They arrive with the worker, in
    their own migration.
    """

    __tablename__ = "invoices"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    product_ref: Mapped[str] = mapped_column(String, nullable=False)
    network: Mapped[str] = mapped_column(String, nullable=False)

    # Snapshot of the configured wallet address at creation time, not a live
    # value: verification compares against this column, so rotating the address
    # in config cannot break invoices that are already open.
    address: Mapped[str] = mapped_column(String, nullable=False)

    invoice_amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="created")

    # Both are filled together, only on the transition into confirmed.
    credited_amount_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    underpaid: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    active_txid: Mapped[str | None] = mapped_column(String, nullable=True)
    attempts_used: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Set when the slot is taken. From this moment the TTL stops being grounds
    # for expiry (TOR section 11 p.1); the bound on waiting becomes
    # MAX_OBSERVATION_WINDOW instead.
    slot_frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    attempts: Mapped[list[InvoiceTxidAttempt]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan"
    )


class InvoiceTxidAttempt(Base):
    """One TXID submission that reached the explorer and got a final verdict.

    A separate table rather than a column on the invoice: there are up to three
    attempts, and global TXID uniqueness is checked here rather than against
    ``invoices.active_txid`` -- otherwise a rejected attempt would leave its
    TXID free to be reused against another invoice.

    Rows exist for four result codes only. ``api_error`` never produces a row
    at all, and a malformed TXID is rejected before the explorer call, so
    neither can appear in ``result_code``.
    """

    __tablename__ = "invoice_txid_attempts"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False
    )

    # Duplicated from the invoice because the unique index is composite.
    network: Mapped[str] = mapped_column(String, nullable=False)
    txid: Mapped[str] = mapped_column(String, nullable=False)
    result_code: Mapped[str] = mapped_column(String, nullable=False)
    from_address: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    invoice: Mapped[Invoice] = relationship(back_populates="attempts")

    __table_args__ = (
        # Partial: what must be globally unique is the fact of *successful* use
        # of a TXID, not the fact of an attempt. The same foreign TXID may be
        # pasted twice and yield already_used twice; only one matched row per
        # (network, txid) can exist.
        #
        # A stalled invoice keeps its matched row, so the TXID stays taken. The
        # service never reopens a stalled invoice by itself: if that money does
        # arrive later, it is a staff matter. Leaving the TXID free instead
        # would allow a second, automatic credit for the same transfer while
        # staff handles the first one by hand.
        Index(
            "uq_txid_attempts_network_txid",
            "network",
            "txid",
            unique=True,
            postgresql_where=text("result_code = 'matched'"),
        ),
    )
