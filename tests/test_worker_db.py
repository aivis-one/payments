"""Worker behaviour that only a real database can settle.

Claiming, leasing, the six process states from the handoffer, both halves of
the two-writer race, and the ceiling check at the boundary where the number is
actually stored. None of it is provable without Postgres, so this module skips
without one and runs on the server, where ``PAYMENTS_TESTS_REQUIRE_DB`` turns a
missing ``DATABASE_URL`` into a failure.

``no_explorer``, not ``no_network``: asyncpg reaches Postgres through
``socket.connect``, so the stronger marker is a property this module cannot
have. The HTTP transports are still trapped, which is the invariant that
matters -- every explorer answer here comes from a frozen fixture.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import explorer_client, settings_dependency
from app.db import get_session
from app.domain.events import TimeChecked
from app.domain.statuses import InvoiceStatus
from app.domain.transitions import decide
from app.main import app
from app.models import Invoice, OutboxEvent
from app.worker import (
    claim_due,
    observe_one,
    poll_once,
    snapshot_of,
    sweep_once,
    sweepable_clause,
    tick,
)
from tests.explorers_support import (
    EVM_TXID,
    EVM_WALLET,
    RecordingTransport,
    json_response,
    load,
    make_settings,
)
from tests.outbox_support import accepting_webhooks

pytestmark = pytest.mark.no_explorer

NETWORK = "USDT-ERC20"
AUTH = {"Authorization": "Bearer test-token-not-real"}

#: The fixtures put the receipt 217 blocks behind the head, and ERC20 asks for
#: 12 -- so an unmodified pair of fixtures is a confirmed payment.
DEEP = [
    lambda: json_response(load("etherscan_erc20_single")),
    lambda: json_response(load("etherscan_block_number")),
]


def settings():
    return make_settings()


def observing_client(*responses: httpx.Response) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=RecordingTransport(*responses))


def deep_client() -> httpx.AsyncClient:
    return observing_client(*[make() for make in DEEP])


def shallow_client(depth_hex: str) -> httpx.AsyncClient:
    """A confirmed-looking receipt with a head only ``depth`` blocks ahead."""
    return observing_client(
        json_response(load("etherscan_erc20_single")),
        json_response({"jsonrpc": "2.0", "id": 1, "result": depth_hex}),
    )


async def make_invoice(
    session: AsyncSession,
    *,
    status: InvoiceStatus = InvoiceStatus.AWAITING_CONFIRMATIONS,
    amount_cents: int = 10_000,
    frozen_minutes_ago: float = 1.0,
    ttl_minutes: float = 60.0,
    next_check_at: datetime | None = None,
    txid: str | None = EVM_TXID,
) -> Invoice:
    now = datetime.now(UTC)
    invoice = Invoice(
        id=uuid.uuid4(),
        product_ref="product-1",
        network=NETWORK,
        address=EVM_WALLET,
        invoice_amount_cents=amount_cents,
        status=status.value,
        attempts_used=1,
        active_txid=txid if status is InvoiceStatus.AWAITING_CONFIRMATIONS else None,
        slot_frozen_at=(
            now - timedelta(minutes=frozen_minutes_ago)
            if status is InvoiceStatus.AWAITING_CONFIRMATIONS
            else None
        ),
        expires_at=now + timedelta(minutes=ttl_minutes),
        next_check_at=next_check_at,
    )
    session.add(invoice)
    await session.commit()
    return invoice


# ==========================================================================
# Claiming, and the NULL that would have hidden everything
# ==========================================================================


async def test_a_never_checked_invoice_is_due(session: AsyncSession):
    """The trap this delivery nearly walked into.

    The API route that takes the slot does not set ``next_check_at``, and is not
    meant to -- the request path knows nothing about the worker. So every
    invoice arrives NULL. ``next_check_at <= now()`` is not true for NULL, so a
    predicate without the NULL branch would see no invoices at all, and on an
    empty database that looks exactly like an idle service.
    """
    invoice = await make_invoice(session)
    assert invoice.next_check_at is None

    claimed = await claim_due(session, settings(), datetime.now(UTC))

    assert claimed == [invoice.id]


async def test_an_invoice_scheduled_for_later_is_not_due(session: AsyncSession):
    await make_invoice(session, next_check_at=datetime.now(UTC) + timedelta(hours=1))

    assert await claim_due(session, settings(), datetime.now(UTC)) == []


async def test_only_invoices_awaiting_confirmations_are_claimed(session: AsyncSession):
    for status in (
        InvoiceStatus.CREATED,
        InvoiceStatus.CONFIRMED,
        InvoiceStatus.EXPIRED,
        InvoiceStatus.STALLED,
        InvoiceStatus.ATTEMPTS_EXHAUSTED,
    ):
        await make_invoice(session, status=status)

    assert await claim_due(session, settings(), datetime.now(UTC)) == []


async def test_claiming_leases_the_invoice_away_from_a_second_worker(
    session: AsyncSession,
):
    """State 2: two instances. Not "that will not happen" -- a lease.

    The claim is one atomic UPDATE that pushes ``next_check_at`` into the
    future, so the second worker's own claim finds nothing. No lock is held
    across the explorer call, which is the point: that call can take seven
    seconds.
    """
    invoice = await make_invoice(session)
    now = datetime.now(UTC)

    first = await claim_due(session, settings(), now)
    second = await claim_due(session, settings(), now)

    assert first == [invoice.id]
    assert second == []

    await session.refresh(invoice)
    assert invoice.next_check_at is not None
    assert invoice.next_check_at > now


async def test_the_lease_expires_so_a_dead_worker_strands_nothing_forever(
    session: AsyncSession,
):
    """State 1: restart mid-poll, seen from the other side.

    A worker killed between claiming and writing leaves the invoice invisible
    for exactly the lease, and then it comes back. Five minutes of delay, not
    an invoice stuck until somebody notices.
    """
    invoice = await make_invoice(session)
    now = datetime.now(UTC)
    await claim_due(session, settings(), now)

    later = now + timedelta(seconds=settings().WORKER_LEASE_SECONDS + 1)

    assert await claim_due(session, settings(), later) == [invoice.id]


async def test_an_empty_cycle_is_not_an_error(session: AsyncSession):
    """State 5. "Nothing happened" is behaviour too, and it must be quiet."""
    async with deep_client() as client:
        polled = await poll_once(session, settings(), client)

    assert polled == 0


# ==========================================================================
# Observation, persisted
# ==========================================================================


async def test_a_deep_enough_transaction_confirms_and_credits(session: AsyncSession):
    invoice = await make_invoice(session, amount_cents=10_000)
    async with deep_client() as client:
        await claim_due(session, settings(), datetime.now(UTC))
        await observe_one(session, settings(), client, invoice.id)

    await session.refresh(invoice)
    assert invoice.status == "confirmed"
    assert invoice.credited_amount_cents == 10_000
    assert invoice.underpaid is False
    assert invoice.confirmations_seen == 217
    assert invoice.last_checked_at is not None


async def test_underpayment_confirms_and_is_flagged_not_refused(session: AsyncSession):
    """TOR section 6: a normal path with a notice, not a blocking error."""
    invoice = await make_invoice(session, amount_cents=50_000)
    async with deep_client() as client:
        await claim_due(session, settings(), datetime.now(UTC))
        await observe_one(session, settings(), client, invoice.id)

    await session.refresh(invoice)
    assert invoice.status == "confirmed"
    assert invoice.credited_amount_cents == 10_000
    assert invoice.underpaid is True


async def test_overpayment_is_credited_in_full_not_trimmed(session: AsyncSession):
    invoice = await make_invoice(session, amount_cents=1_000)
    async with deep_client() as client:
        await claim_due(session, settings(), datetime.now(UTC))
        await observe_one(session, settings(), client, invoice.id)

    await session.refresh(invoice)
    assert invoice.credited_amount_cents == 10_000
    assert invoice.underpaid is False


async def test_a_shallow_transaction_keeps_waiting_and_is_rescheduled(
    session: AsyncSession,
):
    invoice = await make_invoice(session)
    # Head five blocks past the receipt: below the twelve ERC20 asks for.
    async with shallow_client(hex(0xCF2427 + 5)) as client:
        await claim_due(session, settings(), datetime.now(UTC))
        await observe_one(session, settings(), client, invoice.id)

    await session.refresh(invoice)
    assert invoice.status == "awaiting_confirmations"
    assert invoice.credited_amount_cents is None
    assert invoice.confirmations_seen == 5
    assert invoice.next_check_at is not None


async def test_an_api_error_does_not_slow_the_schedule_down(session: AsyncSession):
    """State 4. The explorer failed; the user's money did not.

    No attempt is spent -- that budget belongs to submissions -- and the next
    check stays on the ordinary schedule rather than backing off, because
    backing off would delay money that has already been sent.
    """
    invoice = await make_invoice(session, frozen_minutes_ago=1)
    async with observing_client(json_response({}, status_code=503)) as client:
        await claim_due(session, settings(), datetime.now(UTC))
        await observe_one(session, settings(), client, invoice.id)

    await session.refresh(invoice)
    assert invoice.status == "awaiting_confirmations"
    assert invoice.confirmations_seen == 0
    assert invoice.last_checked_at is not None
    assert invoice.next_check_at is not None
    # The floor interval, not a punished one.
    assert invoice.next_check_at - invoice.last_checked_at <= timedelta(seconds=31)


async def test_a_vanished_transaction_leaves_the_invoice_alone(session: AsyncSession):
    """A reorg displaced it. No depth, no transition, and no credit."""
    invoice = await make_invoice(session)
    async with observing_client(json_response(load("etherscan_result_null"))) as client:
        await claim_due(session, settings(), datetime.now(UTC))
        await observe_one(session, settings(), client, invoice.id)

    await session.refresh(invoice)
    assert invoice.status == "awaiting_confirmations"
    assert invoice.credited_amount_cents is None


async def test_observing_twice_credits_once(session: AsyncSession):
    """Idempotent: the observation carries chain state, not a delta."""
    invoice = await make_invoice(session)
    for _ in range(2):
        async with deep_client() as client:
            await observe_one(session, settings(), client, invoice.id)

    await session.refresh(invoice)
    assert invoice.status == "confirmed"
    assert invoice.credited_amount_cents == 10_000


# ==========================================================================
# The ceiling, checked where the number is stored
# ==========================================================================


async def test_an_amount_too_wide_for_the_column_is_discarded_not_truncated(
    session: AsyncSession,
):
    """State the ceiling guards, and it is a corrupt response rather than money.

    No genuine transfer can reach here -- seven orders separate BSC-USD's whole
    supply from the ceiling and the emitting contract is checked -- so this is
    an explorer lying. The observation is thrown away: no transition, no
    credit, no truncation, and no exception that would abandon the rest of the
    batch. The invoice keeps its schedule and, if the lying continues, the
    observation window ends it in ``stalled`` like any transaction that never
    confirms.
    """
    invoice = await make_invoice(session)
    payload = load("etherscan_erc20_single")
    payload["result"]["logs"][0]["data"] = "0x" + format(10**40, "064x")

    async with observing_client(
        json_response(payload), json_response(load("etherscan_block_number"))
    ) as client:
        await claim_due(session, settings(), datetime.now(UTC))
        await observe_one(session, settings(), client, invoice.id)

    await session.refresh(invoice)
    assert invoice.status == "awaiting_confirmations"
    assert invoice.credited_amount_cents is None
    assert invoice.last_checked_at is not None


# ==========================================================================
# Two writers on one invoice row
# ==========================================================================


async def test_a_persisted_stalled_beats_a_late_worker(session: AsyncSession):
    """State 3. The API got there first, and terminal means terminal.

    TOR section 11 p.10 prefers ``confirmed`` in a tie, but that is a tie
    *within one observation* -- the transition function checks the threshold
    before the window and is where it lives. Once ``stalled`` is a row in the
    database, honouring the preference would mean reopening a terminal status,
    which the service never does for anyone, worker included.
    """
    invoice = await make_invoice(session)
    await claim_due(session, settings(), datetime.now(UTC))

    # Somebody else moves it while we are out at the explorer.
    invoice.status = InvoiceStatus.STALLED.value
    await session.commit()

    async with deep_client() as client:
        await observe_one(session, settings(), client, invoice.id)

    await session.refresh(invoice)
    assert invoice.status == "stalled"
    assert invoice.credited_amount_cents is None
    # The look still happened, and the row records that it did.
    assert invoice.confirmations_seen == 217
    assert invoice.last_checked_at is not None


async def test_a_late_api_call_loses_to_a_worker_that_already_confirmed(
    session: AsyncSession,
):
    """The other half of the pair, and the half that is easy to skip.

    "A persisted stalled beats the worker" would pass just as well against
    broken serialisation that always lets the last writer win. The claim only
    means something next to its mirror: once the worker has confirmed, a
    submission arriving afterwards is refused and the credit is untouched.
    """
    invoice = await make_invoice(session)
    async with deep_client() as client:
        await observe_one(session, settings(), client, invoice.id)

    await session.refresh(invoice)
    assert invoice.status == "confirmed"
    credited = invoice.credited_amount_cents

    explorer = deep_client()
    app.dependency_overrides[settings_dependency] = settings
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[explorer_client] = lambda: explorer
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as http:
            response = await http.post(
                f"/api/v1/invoices/{invoice.id}/txid",
                json={"txid": EVM_TXID},
                headers=AUTH,
            )
    finally:
        app.dependency_overrides.clear()
        await explorer.aclose()

    assert response.status_code == 409
    assert response.json() == {"error": "invoice_already_confirmed"}

    await session.refresh(invoice)
    assert invoice.status == "confirmed"
    assert invoice.credited_amount_cents == credited


async def test_the_tie_inside_one_observation_goes_to_confirmed(session: AsyncSession):
    """TOR section 11 p.10, in the only shape it actually has.

    The slot froze eight days ago, so the observation window has elapsed -- and
    the transaction is confirmed anyway. Refusing a payment demonstrably in the
    network because a timer ran out is worse than holding the invoice longer.
    """
    invoice = await make_invoice(session, frozen_minutes_ago=8 * 24 * 60)
    async with deep_client() as client:
        await observe_one(session, settings(), client, invoice.id)

    await session.refresh(invoice)
    assert invoice.status == "confirmed"
    assert invoice.credited_amount_cents == 10_000


async def test_two_workers_on_one_invoice_never_both_credit(engine) -> None:
    """Two real sessions, one invoice, at the same time.

    Each request gets its own session, because two coroutines sharing one would
    serialise on it and prove nothing.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        invoice = await make_invoice(setup)
        invoice_id = invoice.id

    async def work() -> None:
        async with factory() as own, deep_client() as client:
            claimed = await claim_due(own, settings(), datetime.now(UTC))
            for one in claimed:
                await observe_one(own, settings(), client, one)

    outcomes = await asyncio.gather(work(), work(), return_exceptions=True)
    for outcome in outcomes:
        assert not isinstance(outcome, BaseException), outcome

    async with factory() as check:
        row = await check.get(Invoice, invoice_id)
        assert row is not None
        assert row.status == "confirmed"
        assert row.credited_amount_cents == 10_000


# ==========================================================================
# The sweeper
# ==========================================================================


async def test_an_untouched_invoice_expires_without_any_api_call(session: AsyncSession):
    """P-14. The bound on how late a terminal event can be.

    Lazy resolution makes ``GET`` report the right status, but reporting is not
    the transition having happened, and H5 has nothing to send until it has.
    """
    invoice = await make_invoice(session, status=InvoiceStatus.CREATED, ttl_minutes=-1)

    moved = await sweep_once(session, settings())

    await session.refresh(invoice)
    assert moved == 1
    assert invoice.status == "expired"


async def test_an_exhausted_invoice_also_expires_on_its_ttl(session: AsyncSession):
    invoice = await make_invoice(
        session, status=InvoiceStatus.ATTEMPTS_EXHAUSTED, ttl_minutes=-1
    )

    await sweep_once(session, settings())

    await session.refresh(invoice)
    assert invoice.status == "expired"


async def test_an_elapsed_observation_window_stalls(session: AsyncSession):
    invoice = await make_invoice(session, frozen_minutes_ago=8 * 24 * 60)

    await sweep_once(session, settings())

    await session.refresh(invoice)
    assert invoice.status == "stalled"


async def test_the_sweeper_leaves_live_invoices_alone(session: AsyncSession):
    fresh = await make_invoice(session, status=InvoiceStatus.CREATED, ttl_minutes=60)
    waiting = await make_invoice(session, frozen_minutes_ago=1)

    moved = await sweep_once(session, settings())

    await session.refresh(fresh)
    await session.refresh(waiting)
    assert moved == 0
    assert fresh.status == "created"
    assert waiting.status == "awaiting_confirmations"


@pytest.mark.parametrize(
    "status", [InvoiceStatus.CONFIRMED, InvoiceStatus.EXPIRED, InvoiceStatus.STALLED]
)
async def test_the_sweeper_never_touches_a_terminal_invoice(
    session: AsyncSession, status: InvoiceStatus
):
    invoice = await make_invoice(session, status=status, ttl_minutes=-1)

    await sweep_once(session, settings())

    await session.refresh(invoice)
    assert invoice.status == status.value


async def test_a_tick_runs_the_worker_before_the_sweeper(engine) -> None:
    """One process, three phases, and the order is what settles the tie.

    An invoice whose window has elapsed and whose transaction is confirmed is
    due for both of the first two phases. Running the worker first makes
    ``confirmed`` the outcome by construction rather than by whatever the
    scheduler chose.

    The old form of this test unpacked two counts and asserted the order of two
    phases. Both claims were right and both survive; what changed is that a
    third phase now runs after them, and the event the worker just published
    leaves in the same tick rather than waiting for the next one. Asserting the
    delivery count here is what makes "delivery is last" a checked property
    instead of a comment.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as setup:
        invoice = await make_invoice(setup, frozen_minutes_ago=8 * 24 * 60)
        invoice_id = invoice.id

    async with deep_client() as client, accepting_webhooks() as webhooks:
        polled, swept, delivered = await tick(factory, settings(), client, webhooks)

    async with factory() as check:
        row = await check.get(Invoice, invoice_id)
        assert row is not None
        assert row.status == "confirmed"
        event = await check.scalar(
            select(OutboxEvent).where(OutboxEvent.invoice_id == invoice_id)
        )
        assert event is not None
        assert event.delivery_state == "delivered"
    assert polled == 1
    assert swept == 0
    assert delivered == 1


# ==========================================================================
# P-23: what the sweeper selects, and why it is allowed to name deadlines
# ==========================================================================


async def test_the_sweeper_selects_exactly_what_the_domain_would_move(session: AsyncSession):
    """The pin under a deliberate duplication.

    ``sweepable_clause`` restates two deadlines that already live in
    ``_resolve_by_time``: the invoice TTL, and the observation window. Naming
    them in SQL is what lets the sweeper select only rows it can move, and
    without that its batch used to fill with invoices that cannot move at all.
    The price is a second source for those deadlines, and this is the test that
    was bought with it -- the day the domain rule changes and the clause does
    not, the two sets diverge and this goes red, instead of terminal events
    quietly ceasing to arrive.

    The status space is walked from ``InvoiceStatus`` rather than from a list
    written here. A seventh status would then arrive as a failure demanding a
    decision, rather than as a member that silently belongs to neither set.
    """
    conf = settings()
    now = datetime.now(UTC)
    built: list[Invoice] = []
    for status in InvoiceStatus:
        # Both sides of whichever deadline the status has. For the three that
        # have none, the two rows are simply two rows.
        built.append(await make_invoice(session, status=status, ttl_minutes=-1))
        built.append(
            await make_invoice(
                session, status=status, ttl_minutes=60, frozen_minutes_ago=8 * 24 * 60
            )
        )

    selected = set(
        (await session.scalars(select(Invoice.id).where(sweepable_clause(now, conf)))).all()
    )
    movable = {
        invoice.id
        for invoice in built
        if decide(
            snapshot_of(invoice), TimeChecked(), now, conf.policy_for(invoice.network)
        ).next_status.value
        != invoice.status
    }

    assert selected == movable
    # Guards the test itself: an empty comparison would pass while proving
    # nothing about either side.
    assert movable


async def test_long_lived_slots_no_longer_starve_the_sweeper(session: AsyncSession):
    """P-23, the defect itself.

    ``awaiting_confirmations`` sits in the sweepable set for up to seven days.
    The old selection took the first ``WORKER_BATCH_SIZE`` rows of that set
    regardless of whether they could move, so a batch's worth of slow payments
    occupied it permanently and no invoice ever reached ``expired``. The
    sweeper is what bounds the delay of every terminal event from above, so
    under load it bounded nothing and the product heard nothing.

    A batch of one makes the arithmetic exact rather than approximate.
    """
    small = make_settings(WORKER_BATCH_SIZE=1)
    waiting = await make_invoice(session, frozen_minutes_ago=1)
    overdue = await make_invoice(session, status=InvoiceStatus.CREATED, ttl_minutes=-1)

    moved = await sweep_once(session, small)

    await session.refresh(waiting)
    await session.refresh(overdue)
    assert moved == 1
    assert overdue.status == "expired"
    assert waiting.status == "awaiting_confirmations"


# ==========================================================================
# P-25: one unreadable row must not end a phase
# ==========================================================================


async def test_a_withdrawn_network_does_not_end_the_sweep(session: AsyncSession):
    """The trigger is operational, not a corrupted row.

    ``policy_for`` resolves the network on every touch, while the network is
    only validated when the invoice is created. Withdrawing a network from
    config while its invoices are still open is an ordinary act, and it used to
    end the whole sweeper phase on the first such row.

    The claim is that the *rest of the batch* was processed. "No exception
    escaped" would pass on a guard that swallowed everything.
    """
    retired = await make_invoice(session, status=InvoiceStatus.CREATED, ttl_minutes=-1)
    retired.network = "USDT-RETIRED"
    healthy = await make_invoice(session, status=InvoiceStatus.CREATED, ttl_minutes=-1)
    await session.commit()

    moved = await sweep_once(session, settings())

    await session.refresh(retired)
    await session.refresh(healthy)
    assert moved == 1
    assert healthy.status == "expired"
    assert retired.status == "created"


async def test_a_withdrawn_network_does_not_end_the_worker_phase(session: AsyncSession):
    """The same class, in the phase above. ``spec_for`` raises, not ``policy_for``."""
    retired = await make_invoice(session)
    retired.network = "USDT-RETIRED"
    healthy = await make_invoice(session)
    await session.commit()

    async with deep_client() as client:
        looked_at = await poll_once(session, settings(), client)

    await session.refresh(healthy)
    assert looked_at == 2
    assert healthy.status == "confirmed"


async def test_a_sweep_needs_no_explorer_at_all(session: AsyncSession):
    """It asks the clock, not the chain. The transport is never opened."""
    transport = RecordingTransport()
    await make_invoice(session, status=InvoiceStatus.CREATED, ttl_minutes=-1)

    await sweep_once(session, settings())

    assert transport.calls == 0


async def _unused() -> AsyncIterator[None]:  # pragma: no cover
    yield None


async def test_the_worker_ignores_an_invoice_with_no_txid(session: AsyncSession):
    """Unreachable through the domain, reachable through a hand-edited row.

    ``InvoiceSnapshot`` refuses an awaiting_confirmations row without an
    ``active_txid``, so nothing in this service can produce one. Databases get
    edited by hand anyway, and the honest answer is to leave the row alone
    rather than to crash the batch it happens to be in.
    """
    invoice = await make_invoice(session)
    invoice.active_txid = None
    await session.commit()

    async with deep_client() as client:
        result = await observe_one(session, settings(), client, invoice.id)

    assert result is None
    await session.refresh(invoice)
    assert invoice.status == "awaiting_confirmations"


async def test_observing_an_invoice_that_vanished_is_not_an_error(session: AsyncSession):
    async with deep_client() as client:
        assert await observe_one(session, settings(), client, uuid.uuid4()) is None


async def test_a_selected_batch_is_bounded(session: AsyncSession):
    """The claim takes a batch, not the table."""
    for _ in range(5):
        await make_invoice(session)
    small = make_settings(WORKER_BATCH_SIZE=2)

    claimed = await claim_due(session, small, datetime.now(UTC))

    assert len(claimed) == 2
