"""ORM models: ``invoices`` and ``invoice_txid_attempts`` (TOR section 4), and
``outbox_events`` (TOR section 8).

Status and result-code columns are plain ``String``, not ``SAEnum`` and without
a ``CHECK``: that is the project convention for status/type columns, and it is
also what keeps the network list open (TOR section 12).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Invoice(Base):
    """One top-up request: amount, address snapshot, lifecycle status.

    The three worker fields arrived with the worker, in their own migration,
    exactly as TOR section 4 said they would: columns nobody reads or writes
    are worse than absent ones.
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

    # -- worker bookkeeping (TOR section 4) ---------------------------------
    #
    # The last depth the worker read. Kept even though the decision is made
    # from the fresh observation: without it, "nothing has changed since the
    # last look" is not a question anyone can ask of the row.
    confirmations_seen: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # When this invoice becomes due for another look.
    #
    # **NULL means "never looked at", and that is the permanent meaning of the
    # column, not a migration artefact.** The API route that moves an invoice
    # into ``awaiting_confirmations`` does not set this field and is not meant
    # to: the request path stays unaware that a worker exists. So every invoice
    # arrives here NULL and the worker's selection has to read NULL as due now.
    # A predicate simplified to ``next_check_at <= now()`` would be false for
    # NULL and the worker would see no invoices at all -- on an empty database
    # that failure looks exactly like an idle service.
    next_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    attempts: Mapped[list[InvoiceTxidAttempt]] = relationship(
        back_populates="invoice", cascade="all, delete-orphan"
    )


class OutboxEvent(Base):
    """One outgoing event, written in the transaction that made it true.

    The row is created by the same commit that moves the invoice into a
    published status (TOR section 8: ``confirmed``, ``expired``,
    ``attempts_exhausted``, ``stalled``). That is the whole point of the table:
    a crash between the status and the event is not a state the service can
    reach, so an event cannot be lost. Delivery is a separate, later, retrying
    thing -- late is allowed, lost is not.

    Two counters, and they are not the same question:

    * ``attempts`` counts *finished* deliveries -- a response arrived, or the
      network refused. It is the one that leads to ``failed``.
    * ``claims`` counts times the row was taken for delivery. A process killed
      mid-POST, or a defect that throws before any request is made, raises this
      one and not the other, so ``claims - attempts`` is exactly the set of
      deliveries that started and never reported. Without it such a row is
      indistinguishable from a fresh one and shows up only as an error line
      repeated forever in a log nobody reads.
    """

    __tablename__ = "outbox_events"

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False
    )

    # Two different meanings of "status" would otherwise share one name in one
    # table: this is the invoice's, and ``delivery_state`` is the row's own.
    invoice_status: Mapped[str] = mapped_column(String, nullable=False)

    # The wire body, frozen at publication. Delivery sends this and never
    # rebuilds it from the invoice: by the time it goes out the invoice may
    # have moved on, and the event describes the transition, not the present.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)

    # The moment of the transition, not of the delivery.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    delivery_state: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    claims: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )

    # When the row is next due. NOT NULL and set to the publication time, so
    # "due now" is a date in the past rather than a NULL -- the column is both
    # the schedule and the lease, and a NULL in either role compares to neither
    # true nor false.
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Why the last attempt did not succeed. Without it ``failed`` is a state
    # with no explanation, and the operator's only recourse is the log.
    last_error: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        # The product deduplicates on (invoice_id, status) -- TOR section 8 --
        # so the same pair is the natural identity of an event here too, and
        # making it unique closes double publication in the schema instead of
        # in a branch. It is reachable: the API does not lock the invoice row,
        # so two requests can both see ``created`` and both write ``expired``.
        #
        # Nothing legal is refused by it. Statuses are entered once: an invoice
        # that goes attempts_exhausted -> expired produces two rows, because
        # the pairs differ.
        Index(
            "uq_outbox_events_invoice_status",
            "invoice_id",
            "invoice_status",
            unique=True,
        ),
        # The delivery loop's only selection.
        Index(
            "ix_outbox_events_due",
            "next_attempt_at",
            postgresql_where=text("delivery_state = 'pending'"),
        ),
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
