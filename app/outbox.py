"""The outgoing half of TOR section 8: publish an event, then deliver it.

**Publication is transactional and delivery is not.** ``publish`` adds a row to
the transaction that is already changing the invoice, so the two commit or fail
together; ``deliver_once`` picks rows up afterwards, in its own transactions,
and retries. The rule the whole design is bent around is that delivery may be
late but may not be lost -- the product deduplicates on ``(invoice_id,
status)``, so a duplicate costs it nothing and a loss costs it a payment.

**Publication happens on entering a status, from every place that writes one.**
There are five such places in this service -- three in ``app.api.routes`` and
two in ``app.worker`` -- and an event that only left one of them would leave the
product silent for half of the transitions.

**Two delivery loops are safe by construction.** A claim is an atomic
``UPDATE ... RETURNING`` that pushes ``next_attempt_at`` into the future, the
same device the confirmations worker uses on ``next_check_at``; a second loop's
claim finds nothing to take. It holds no lock across the HTTP call, which is
what makes it usable at all.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import httpx
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.domain.statuses import InvoiceStatus
from app.models import Invoice, OutboxEvent

log = logging.getLogger("payments.outbox")


def _now() -> datetime:
    return datetime.now(UTC)


class DeliveryState(StrEnum):
    """The three states of an outbox row (TOR section 8)."""

    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


#: The four statuses the product is told about (TOR section 8).
#:
#: Deliberately not ``TERMINAL_STATUSES``: that set is three, because
#: ``attempts_exhausted`` still waits for its TTL and is not terminal. It is
#: still an event -- the product needs to stop showing a payment form for an
#: invoice that will accept no more hashes.
PUBLISHED_STATUSES: frozenset[InvoiceStatus] = frozenset(
    {
        InvoiceStatus.CONFIRMED,
        InvoiceStatus.EXPIRED,
        InvoiceStatus.ATTEMPTS_EXHAUSTED,
        InvoiceStatus.STALLED,
    }
)


def event_payload(invoice: Invoice, occurred_at: datetime) -> dict[str, Any]:
    """The wire body of TOR section 8, frozen at the moment of the transition.

    ``credited_amount_cents`` and ``underpaid`` are *absent* rather than
    ``null`` when the transition did not set them -- they are optional in the
    contract, and the receiver is expected to test for the key, not for
    falsiness. ``underpaid`` is a boolean whose false value is meaningful, so
    the difference matters.

    JSONB stores neither UUIDs nor datetimes, so both are rendered here: the id
    as its string form, the moment as ISO-8601 with an offset.
    """
    payload: dict[str, Any] = {
        "invoice_id": str(invoice.id),
        "product_ref": invoice.product_ref,
        "status": invoice.status,
    }
    if invoice.credited_amount_cents is not None:
        payload["credited_amount_cents"] = invoice.credited_amount_cents
    if invoice.underpaid is not None:
        payload["underpaid"] = invoice.underpaid
    payload["occurred_at"] = occurred_at.isoformat()
    return payload


async def publish(
    session: AsyncSession,
    invoice: Invoice,
    *,
    previous: InvoiceStatus,
    occurred_at: datetime,
) -> None:
    """Add the event for this transition to the caller's transaction.

    Does not commit: the caller owns the transaction, and that ownership is the
    guarantee. Called after the new status is on the row, so that the frozen
    payload carries the effects the transition set.

    ``occurred_at`` is passed in rather than read from the clock here. It is the
    moment the transition was decided, which is not the moment it is delivered
    and, for ``attempts_exhausted``, not the moment the budget actually ran out
    either -- the concurrency contract of TOR section 11 p.8 lets the counter
    pass the threshold while the status still says ``created``, and the status
    follows at the next touch. Late, and honest about being late.

    ``ON CONFLICT DO NOTHING`` rather than a check-then-insert: two writers can
    both see ``created`` and both decide ``expired``, because the API does not
    lock the invoice row. A plain INSERT would raise ``IntegrityError`` and take
    the caller's whole transaction -- the status change included -- down with
    it.
    """
    current = InvoiceStatus(invoice.status)
    if current is previous or current not in PUBLISHED_STATUSES:
        return

    await session.execute(
        insert(OutboxEvent)
        .values(
            id=uuid.uuid4(),
            invoice_id=invoice.id,
            invoice_status=current.value,
            payload=event_payload(invoice, occurred_at),
            occurred_at=occurred_at,
            delivery_state=DeliveryState.PENDING.value,
            next_attempt_at=occurred_at,
        )
        .on_conflict_do_nothing(index_elements=["invoice_id", "invoice_status"])
    )


def backoff(attempts: int, settings: Settings) -> timedelta:
    """How long to wait before the next try, doubling up to a ceiling.

    Capped before the shift, so that a row with a large count does not compute
    an astronomical number on its way to being clamped.
    """
    steps = min(max(attempts - 1, 0), 32)
    seconds = settings.WEBHOOK_BACKOFF_MIN_SECONDS * float(2**steps)
    return timedelta(seconds=min(seconds, settings.WEBHOOK_BACKOFF_MAX_SECONDS))


async def claim_due(
    session: AsyncSession, settings: Settings, now: datetime
) -> list[uuid.UUID]:
    """Take a batch of due events, making them invisible to other loops.

    The claim raises ``claims`` and pushes ``next_attempt_at`` one lease into
    the future in the same statement, so a row taken by a process that then dies
    comes back on its own -- that is what makes delivery survive a restart
    without a recovery pass.

    ``ORDER BY next_attempt_at`` is what stops one row monopolising the batch:
    a row that keeps being taken keeps pushing itself to the back of the queue.
    """
    due = (
        select(OutboxEvent.id)
        .where(
            OutboxEvent.delivery_state == DeliveryState.PENDING.value,
            OutboxEvent.next_attempt_at <= now,
        )
        .order_by(OutboxEvent.next_attempt_at)
        .limit(settings.WORKER_BATCH_SIZE)
        .with_for_update(skip_locked=True)
    )
    claimed = await session.scalars(
        update(OutboxEvent)
        .where(OutboxEvent.id.in_(due.scalar_subquery()))
        .values(
            claims=OutboxEvent.claims + 1,
            next_attempt_at=now + timedelta(seconds=settings.WEBHOOK_LEASE_SECONDS),
        )
        .returning(OutboxEvent.id)
    )
    ids = list(claimed)
    await session.commit()
    return ids


async def deliver_one(
    session: AsyncSession,
    settings: Settings,
    client: httpx.AsyncClient,
    event_id: uuid.UUID,
) -> bool | None:
    """POST one event and record what came back.

    Returns True when the product accepted it, False when the attempt was spent
    without success, and None when there was nothing to deliver.

    **Any answer that is not 2xx is retried, including 4xx.** There is no class
    of permanent failures here on purpose: a 404 from a product that is halfway
    through a deployment is indistinguishable from a 404 at a misconfigured
    path, and only one of the two is worth giving up on. A wrong URL therefore
    costs twelve attempts before the row reaches ``failed`` -- and names itself
    in ``last_error`` when it gets there.
    """
    event = await session.get(OutboxEvent, event_id)
    if event is None:
        return None

    try:
        response = await client.post(
            settings.PRODUCT_WEBHOOK_URL,
            json=event.payload,
            headers={"X-Payments-Secret": settings.PAYMENTS_WEBHOOK_SECRET},
            timeout=settings.WEBHOOK_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        return await _spend_attempt(session, settings, event, f"{type(exc).__name__}: {exc}")

    if 200 <= response.status_code < 300:
        # The body is not read at all. The contract is the status code; a
        # product that answers 204, or 200 with nothing in it, has accepted the
        # event just as much as one that echoes it back.
        event.delivery_state = DeliveryState.DELIVERED.value
        event.attempts += 1
        event.last_error = None
        await session.commit()
        return True

    return await _spend_attempt(session, settings, event, f"HTTP {response.status_code}")


async def _spend_attempt(
    session: AsyncSession, settings: Settings, event: OutboxEvent, error: str
) -> bool:
    """Charge one attempt to the budget and either reschedule or give up."""
    event.attempts += 1
    event.last_error = error

    if event.attempts >= settings.WEBHOOK_MAX_ATTEMPTS:
        event.delivery_state = DeliveryState.FAILED.value
        log.error(
            "outbox %s (%s) failed after %d attempts: %s",
            event.id,
            event.invoice_status,
            event.attempts,
            error,
        )
    else:
        event.next_attempt_at = _now() + backoff(event.attempts, settings)

    await session.commit()
    return False


async def _cool_down(
    session: AsyncSession, settings: Settings, event_id: uuid.UUID
) -> None:
    """Push back a row whose delivery threw instead of finishing.

    The budget is untouched, deliberately. An exception from our own code is
    not an answer from the product, and spending the budget on it would mark a
    row ``failed`` while the product is healthy -- the error in the direction
    the whole design refuses to make.

    The schedule is computed from ``claims`` rather than ``attempts``, so a row
    that throws every time still backs off to the ceiling instead of being
    retried every lease forever. That bounds the log to about a dozen lines a
    day per row, and ``claims - attempts`` finds the row with one query.
    """
    event = await session.get(OutboxEvent, event_id)
    if event is None:
        return
    event.next_attempt_at = _now() + backoff(event.claims - event.attempts, settings)
    await session.commit()


async def deliver_once(
    session: AsyncSession, settings: Settings, client: httpx.AsyncClient
) -> int:
    """One delivery phase. Returns how many events were taken.

    The per-row guard begins with a rollback, and that is not decoration: an
    exception raised inside a database call leaves the session dirty, and
    without the rollback every remaining row in the batch would fail on the
    poisoned transaction rather than on its own merits.
    """
    claimed = await claim_due(session, settings, _now())
    for event_id in claimed:
        try:
            await deliver_one(session, settings, client, event_id)
        except Exception:
            await session.rollback()
            log.exception("outbox %s could not be delivered", event_id)
            await _cool_down(session, settings, event_id)
    return len(claimed)
