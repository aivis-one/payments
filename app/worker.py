"""The background process: confirmations worker, then sweeper, in one loop.

**One process, two phases, worker first.** The two could have been separate
services, and the cheap argument for merging them is that H6 then has one entry
in its compose file instead of two. The real argument is ordering. Both phases
can write the same invoice row -- the worker moves it to ``confirmed``, the
sweeper moves it to ``stalled`` -- and running them in one loop makes which of
them gets there first a property of this file rather than of whatever the
scheduler felt like doing. TOR section 11 p.10 prefers ``confirmed``, so the
worker runs first.

**Nothing here emits events.** The transitions are produced, the outbox and the
outgoing webhook are H5. A status reaching ``confirmed`` in the database with
nobody told about it yet is the intended intermediate state, not a gap.

**Two-writer safety, three mechanisms, each doing a different job.**

* *Claiming* is an atomic ``UPDATE ... RETURNING`` that pushes ``next_check_at``
  into the future. A second worker's own claim finds nothing to take. This is
  what stops two processes polling one invoice, and it costs no held lock --
  which matters because the explorer call that follows can take seven seconds
  and must not happen inside a transaction.
* *Re-reading under ``FOR UPDATE``* before writing is what stops the worker
  overwriting a decision somebody else made while it was out at the explorer.
  It serialises against the API's own ``UPDATE`` too, not merely against other
  readers.
* *The transition function itself* refuses any snapshot that is no longer
  ``awaiting_confirmations``. Once the re-read is fresh, that refusal is the
  correct answer rather than a lost update: a persisted ``stalled`` is terminal
  for everyone, the worker included.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db import get_session_factory
from app.domain.events import ConfirmationsObserved, InvoiceSnapshot, TimeChecked
from app.domain.statuses import InvoiceStatus
from app.domain.transitions import Decision, decide
from app.explorers.registry import spec_for
from app.models import Invoice

log = logging.getLogger("payments.worker")

#: Widest value a PostgreSQL ``bigint`` column holds.
#:
#: ``raw_to_cents`` is unbounded and correct that way -- a pure function has no
#: business knowing about column widths. The check belongs where the number is
#: persisted, which is here.
BIGINT_MAX: int = 2**63 - 1

#: Statuses the sweeper can move by the clock alone.
SWEEPABLE: tuple[str, ...] = (
    InvoiceStatus.CREATED.value,
    InvoiceStatus.ATTEMPTS_EXHAUSTED.value,
    InvoiceStatus.AWAITING_CONFIRMATIONS.value,
)


def _now() -> datetime:
    return datetime.now(UTC)


def snapshot_of(invoice: Invoice) -> InvoiceSnapshot:
    return InvoiceSnapshot(
        status=InvoiceStatus(invoice.status),
        invoice_amount_cents=invoice.invoice_amount_cents,
        attempts_used=invoice.attempts_used,
        expires_at=invoice.expires_at,
        slot_frozen_at=invoice.slot_frozen_at,
        active_txid=invoice.active_txid,
    )


def poll_interval(age: timedelta, settings: Settings) -> timedelta:
    """How long to wait before looking at a slot of this age again.

    Doubling every ten minutes from the floor up to the ceiling. Ten minutes is
    chosen because it comfortably covers the confirmation time of all three
    networks, so a transaction that is going to confirm normally is polled at
    the fast rate for its whole life and never sees the backoff at all.
    """
    periods = max(0, int(age.total_seconds() // 600))
    # Capped before the shift so that a week-old slot does not compute an
    # astronomically large number on the way to being clamped.
    seconds = settings.WORKER_POLL_MIN_SECONDS * float(2 ** min(periods, 32))
    return timedelta(seconds=min(seconds, settings.WORKER_POLL_MAX_SECONDS))


async def claim_due(
    session: AsyncSession, settings: Settings, now: datetime
) -> list[uuid.UUID]:
    """Take a batch of due invoices, making them invisible to other workers.

    ``next_check_at IS NULL`` is part of the predicate and stays part of it. The
    API route that moves an invoice into ``awaiting_confirmations`` does not set
    the field -- deliberately, so the request path knows nothing about the
    worker -- so every invoice arrives here NULL. Simplify this to
    ``next_check_at <= now`` and NULL compares to neither true nor false, the
    worker sees nothing at all, and an empty database makes that look like an
    idle service rather than a broken one.
    """
    due = (
        select(Invoice.id)
        .where(
            Invoice.status == InvoiceStatus.AWAITING_CONFIRMATIONS.value,
            (Invoice.next_check_at.is_(None)) | (Invoice.next_check_at <= now),
        )
        .order_by(Invoice.next_check_at.nulls_first())
        .limit(settings.WORKER_BATCH_SIZE)
        .with_for_update(skip_locked=True)
    )
    claimed = await session.scalars(
        update(Invoice)
        .where(Invoice.id.in_(due.scalar_subquery()))
        .values(next_check_at=now + timedelta(seconds=settings.WORKER_LEASE_SECONDS))
        .returning(Invoice.id)
    )
    ids = list(claimed)
    await session.commit()
    return ids


async def observe_one(
    session: AsyncSession,
    settings: Settings,
    client: httpx.AsyncClient,
    invoice_id: uuid.UUID,
) -> Decision | None:
    """Look at one claimed invoice and persist what it means.

    Returns the decision that was applied, or ``None`` when nothing was --
    which covers an invoice that moved on without us, an explorer that could
    not answer, and an amount too large to store.
    """
    invoice = await session.get(Invoice, invoice_id)
    if invoice is None:
        return None

    adapter = spec_for(invoice.network).build(settings, client)
    txid = invoice.active_txid
    if txid is None:
        # Unreachable through the domain: InvoiceSnapshot refuses an
        # awaiting_confirmations row without an active_txid. Reachable through
        # a hand-edited database, which is a real thing that happens to
        # production, and the honest answer is to leave the row alone.
        log.warning("invoice %s is awaiting confirmations with no txid", invoice_id)
        return None

    observation = await adapter.observe(txid, invoice.address)
    await session.rollback()  # the read above holds nothing across the call

    if observation.confirmations is None:
        # api_error, or the transaction is no longer where we left it. Neither
        # spends a user attempt -- that budget is for submissions, and this is
        # not one -- and neither slows the schedule down: backing off because
        # the explorer failed would delay money the user has already sent.
        await _reschedule(session, invoice_id, settings)
        return None

    return await _persist(session, settings, invoice_id, observation.confirmations,
                          observation.result.raw_amount or 0)


async def _persist(
    session: AsyncSession,
    settings: Settings,
    invoice_id: uuid.UUID,
    confirmations: int,
    raw_amount: int,
) -> Decision | None:
    """Re-read under a row lock, decide on the fresh state, write once."""
    invoice = await session.scalar(
        select(Invoice).where(Invoice.id == invoice_id).with_for_update()
    )
    if invoice is None:
        await session.rollback()
        return None

    now = _now()
    if InvoiceStatus(invoice.status) is not InvoiceStatus.AWAITING_CONFIRMATIONS:
        # Somebody moved it while we were at the explorer. Whatever they wrote
        # stands: the only status we could be racing with here is terminal, and
        # terminal means terminal for the worker too.
        invoice.confirmations_seen = confirmations
        invoice.last_checked_at = now
        await session.commit()
        return None

    decision = decide(
        snapshot_of(invoice),
        ConfirmationsObserved(confirmations=confirmations, raw_amount=raw_amount),
        now,
        settings.policy_for(invoice.network),
    )

    credited = decision.effects.credited_amount_cents
    if credited is not None and credited > BIGINT_MAX:
        # Not an accounting case. Seven orders of magnitude separate the total
        # supply of BSC-USD from this ceiling, and the emitting contract is
        # checked, so a genuine payment cannot reach it -- only a corrupted or
        # hostile response can. So the observation is discarded rather than
        # acted on: no transition, no credit, no truncation, and no crash that
        # would stop every other invoice in the batch. The invoice keeps its
        # schedule and, if the explorer keeps lying, ends up ``stalled`` by the
        # observation window like any other transaction that never confirms.
        log.error(
            "invoice %s: credited amount %d exceeds bigint; observation discarded",
            invoice_id,
            credited,
        )
        invoice.last_checked_at = now
        await session.commit()
        return None

    invoice.confirmations_seen = confirmations
    invoice.last_checked_at = now
    invoice.status = decision.next_status.value
    if decision.effects.credited_amount_cents is not None:
        invoice.credited_amount_cents = decision.effects.credited_amount_cents
    if decision.effects.underpaid is not None:
        invoice.underpaid = decision.effects.underpaid
    if decision.next_status is InvoiceStatus.AWAITING_CONFIRMATIONS:
        age = now - invoice.slot_frozen_at if invoice.slot_frozen_at else timedelta(0)
        invoice.next_check_at = now + poll_interval(age, settings)

    # Status and bookkeeping in one transaction, which is what makes "wrote the
    # status but not last_checked_at" an unreachable state rather than a branch
    # somebody has to handle after a restart.
    await session.commit()
    return decision


async def _reschedule(
    session: AsyncSession, invoice_id: uuid.UUID, settings: Settings
) -> None:
    """Put an unreadable invoice back on the ordinary schedule."""
    invoice = await session.get(Invoice, invoice_id)
    if invoice is None:
        return
    now = _now()
    age = now - invoice.slot_frozen_at if invoice.slot_frozen_at else timedelta(0)
    invoice.last_checked_at = now
    invoice.next_check_at = now + poll_interval(age, settings)
    await session.commit()


async def poll_once(
    session: AsyncSession, settings: Settings, client: httpx.AsyncClient
) -> int:
    """One worker phase. Returns how many invoices were looked at."""
    claimed = await claim_due(session, settings, _now())
    for invoice_id in claimed:
        await observe_one(session, settings, client, invoice_id)
    return len(claimed)


async def sweep_once(session: AsyncSession, settings: Settings) -> int:
    """One sweeper phase: terminal statuses for invoices nobody is touching.

    This is what bounds the delay of every terminal event from above. Without
    it an invoice that nobody ever calls again would sit in ``created`` past
    its TTL indefinitely -- the API resolves the clock lazily and reports the
    right status, but reporting is not the same as the transition having
    happened, and H5 has nothing to send until it has.

    No explorer is involved: the sweeper asks the clock, not the chain.
    """
    now = _now()
    invoices = (
        await session.scalars(
            select(Invoice)
            .where(Invoice.status.in_(SWEEPABLE))
            .limit(settings.WORKER_BATCH_SIZE)
            .with_for_update(skip_locked=True)
        )
    ).all()

    moved = 0
    for invoice in invoices:
        decision = decide(
            snapshot_of(invoice), TimeChecked(), now, settings.policy_for(invoice.network)
        )
        if decision.next_status.value != invoice.status:
            invoice.status = decision.next_status.value
            moved += 1
    await session.commit()
    return moved


async def tick(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    client: httpx.AsyncClient,
) -> tuple[int, int]:
    """One full cycle: worker phase, then sweeper phase. Returns both counts."""
    async with session_factory() as session:
        polled = await poll_once(session, settings, client)
    async with session_factory() as session:
        swept = await sweep_once(session, settings)
    return polled, swept


async def run() -> None:  # pragma: no cover - the loop itself is not unit-tested
    """Run ticks forever."""
    settings = get_settings()
    factory = get_session_factory()
    async with httpx.AsyncClient() as client:
        while True:
            try:
                polled, swept = await tick(factory, settings, client)
                if polled or swept:
                    log.info("tick: polled=%d swept=%d", polled, swept)
            except Exception:
                # One bad tick must not end the process: the next one may well
                # succeed, and a worker that exits on the first transient
                # failure stops every invoice rather than one.
                log.exception("tick failed")
            await asyncio.sleep(settings.WORKER_TICK_SECONDS)


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())
