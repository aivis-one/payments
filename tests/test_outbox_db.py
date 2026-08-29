"""Outbox behaviour that only a real database can settle.

The unique index, the ``ON CONFLICT DO NOTHING`` that keeps a racing publisher
from taking down the transaction it rode in on, the claim that survives a
restart -- none of it is provable against a stub. A stub can be told to raise
``IntegrityError``; only the index can demonstrate that the database would.

``no_explorer``, not ``no_network``: asyncpg reaches Postgres through
``socket.connect``, so the stronger marker is a property this module cannot
have. The HTTP transports are still trapped, so neither an explorer nor the
product is reachable except through the mock transports below.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import explorer_client, settings_dependency
from app.db import get_session
from app.domain.statuses import InvoiceStatus
from app.main import app
from app.models import Invoice, OutboxEvent
from app.outbox import DeliveryState, claim_due, deliver_once, publish
from app.worker import poll_once, sweep_once
from tests.explorers_support import (
    EVM_TXID,
    EVM_WALLET,
    RecordingTransport,
    json_response,
    load,
    make_settings,
)
from tests.outbox_support import WEBHOOK_SECRET, WEBHOOK_URL, WebhookTransport, webhook_client

pytestmark = pytest.mark.no_explorer

NETWORK = "USDT-ERC20"
AUTH = {"Authorization": "Bearer test-token-not-real"}

#: A receipt 217 blocks behind the head, against a threshold of 12: confirmed.
DEEP = [
    lambda: json_response(load("etherscan_erc20_single")),
    lambda: json_response(load("etherscan_block_number")),
]


def settings(**overrides: object):
    return make_settings(**overrides)


def deep_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=RecordingTransport(*[make() for make in DEEP]))


async def make_invoice(
    session: AsyncSession,
    *,
    status: InvoiceStatus = InvoiceStatus.CREATED,
    attempts_used: int = 0,
    ttl_minutes: float = 60.0,
    frozen_minutes_ago: float | None = None,
    txid: str | None = None,
) -> Invoice:
    now = datetime.now(UTC)
    awaiting = status is InvoiceStatus.AWAITING_CONFIRMATIONS
    invoice = Invoice(
        id=uuid.uuid4(),
        product_ref="product-1",
        network=NETWORK,
        address=EVM_WALLET,
        invoice_amount_cents=10_000,
        status=status.value,
        attempts_used=attempts_used,
        active_txid=(txid or EVM_TXID) if awaiting else None,
        slot_frozen_at=(
            now - timedelta(minutes=frozen_minutes_ago if frozen_minutes_ago else 1.0)
            if awaiting
            else None
        ),
        expires_at=now + timedelta(minutes=ttl_minutes),
    )
    session.add(invoice)
    await session.commit()
    return invoice


async def events_for(session: AsyncSession, invoice_id: uuid.UUID) -> list[OutboxEvent]:
    rows = await session.scalars(
        select(OutboxEvent)
        .where(OutboxEvent.invoice_id == invoice_id)
        .order_by(OutboxEvent.created_at)
    )
    return list(rows)


async def only_event(session: AsyncSession, invoice_id: uuid.UUID) -> OutboxEvent:
    rows = await events_for(session, invoice_id)
    assert len(rows) == 1, [row.invoice_status for row in rows]
    return rows[0]


def route_client(session: AsyncSession, *responses: httpx.Response) -> httpx.AsyncClient:
    explorer = httpx.AsyncClient(transport=RecordingTransport(*responses))
    conf = settings()
    app.dependency_overrides[settings_dependency] = lambda: conf
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[explorer_client] = lambda: explorer
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


# ==========================================================================
# Both sources, separately -- because one of them silently missing is the
# failure this table exists to prevent
# ==========================================================================


async def test_a_route_publishes_the_event_it_persists(session: AsyncSession):
    """The first of the two sources. Three of the four statuses are born here.

    Published by a *refusal*, which is the case easiest to drop: the request is
    answered 409, and it is not obvious from the outside that a status changed
    at all.
    """
    invoice = await make_invoice(session, ttl_minutes=-1)

    async with route_client(session) as http:
        response = await http.post(
            f"/api/v1/invoices/{invoice.id}/txid", json={"txid": EVM_TXID}, headers=AUTH
        )

    assert response.status_code == 409
    event = await only_event(session, invoice.id)
    assert event.invoice_status == "expired"
    assert event.delivery_state == "pending"


async def test_the_worker_publishes_the_event_it_persists(session: AsyncSession):
    """The second source. Separate test, not a parametrisation of the first.

    The two write through entirely different code -- one under a row lock after
    a re-read, one without any lock at all -- and a parametrised pair would
    share a body that could only exercise one of them.
    """
    invoice = await make_invoice(
        session, status=InvoiceStatus.AWAITING_CONFIRMATIONS
    )

    async with deep_client() as client:
        await poll_once(session, settings(), client)

    event = await only_event(session, invoice.id)
    assert event.invoice_status == "confirmed"


async def test_the_sweeper_publishes_the_event_it_persists(session: AsyncSession):
    """The third writer, and the one that bounds every delay from above."""
    invoice = await make_invoice(session, ttl_minutes=-1)

    await sweep_once(session, settings())

    event = await only_event(session, invoice.id)
    assert event.invoice_status == "expired"


async def test_a_transition_leaves_no_source_without_an_event(session: AsyncSession):
    """The done-when, stated as one assertion over both writers at once."""
    from_route = await make_invoice(session, ttl_minutes=-1)
    from_sweeper = await make_invoice(session, ttl_minutes=-1)

    async with route_client(session) as http:
        await http.post(
            f"/api/v1/invoices/{from_route.id}/txid",
            json={"txid": EVM_TXID},
            headers=AUTH,
        )
    await sweep_once(session, settings())

    assert len(await events_for(session, from_route.id)) == 1
    assert len(await events_for(session, from_sweeper.id)) == 1


# ==========================================================================
# Four statuses, one test each
# ==========================================================================


async def test_confirmed_is_published(session: AsyncSession):
    invoice = await make_invoice(session, status=InvoiceStatus.AWAITING_CONFIRMATIONS)

    async with deep_client() as client:
        await poll_once(session, settings(), client)

    event = await only_event(session, invoice.id)
    assert event.invoice_status == "confirmed"
    assert event.payload["credited_amount_cents"] == 10_000
    assert event.payload["underpaid"] is False


async def test_expired_is_published(session: AsyncSession):
    invoice = await make_invoice(session, ttl_minutes=-1)

    await sweep_once(session, settings())

    assert (await only_event(session, invoice.id)).invoice_status == "expired"


async def test_stalled_is_published(session: AsyncSession):
    invoice = await make_invoice(
        session,
        status=InvoiceStatus.AWAITING_CONFIRMATIONS,
        frozen_minutes_ago=8 * 24 * 60,
    )

    await sweep_once(session, settings())

    assert (await only_event(session, invoice.id)).invoice_status == "stalled"


async def test_attempts_exhausted_is_published(session: AsyncSession):
    """The fourth, and the one a terminal-statuses shortcut would have lost.

    Reached through a budget that shrank under a live invoice, which is the
    path that produces this status without an explorer call.
    """
    invoice = await make_invoice(session, attempts_used=5)

    async with route_client(session) as http:
        response = await http.post(
            f"/api/v1/invoices/{invoice.id}/txid", json={"txid": EVM_TXID}, headers=AUTH
        )

    assert response.json() == {"error": "attempts_exhausted"}
    assert (await only_event(session, invoice.id)).invoice_status == "attempts_exhausted"


async def test_a_status_that_is_not_an_event_writes_no_row(session: AsyncSession):
    """The negative half. ``created`` and ``awaiting_confirmations`` are not events."""
    invoice = await make_invoice(session)

    async with route_client(session, json_response(load("etherscan_erc20_single"))) as http:
        response = await http.post(
            f"/api/v1/invoices/{invoice.id}/txid", json={"txid": EVM_TXID}, headers=AUTH
        )

    assert response.json()["status"] == "awaiting_confirmations"
    assert await events_for(session, invoice.id) == []


async def test_two_statuses_of_one_invoice_are_two_events(session: AsyncSession):
    """``attempts_exhausted`` then ``expired``: the pair differs, so both go.

    This is what the unique index must not refuse, and the reason it is on the
    pair rather than on the invoice.
    """
    invoice = await make_invoice(session, attempts_used=5)

    async with route_client(session) as http:
        await http.post(
            f"/api/v1/invoices/{invoice.id}/txid", json={"txid": EVM_TXID}, headers=AUTH
        )
    invoice.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await session.commit()
    await sweep_once(session, settings())

    assert [event.invoice_status for event in await events_for(session, invoice.id)] == [
        "attempts_exhausted",
        "expired",
    ]


# ==========================================================================
# The repetition axis: publishing the same transition twice
# ==========================================================================


async def test_publishing_the_same_pair_twice_leaves_one_row(session: AsyncSession):
    """Reachable, not hypothetical: the API takes no lock on the invoice row.

    Two requests can both read ``created``, both resolve ``expired``, and both
    publish. A plain INSERT would raise and take the caller's whole transaction
    -- the status change included -- down with it.
    """
    invoice = await make_invoice(session, ttl_minutes=-1)
    now = datetime.now(UTC)
    invoice.status = InvoiceStatus.EXPIRED.value

    await publish(session, invoice, previous=InvoiceStatus.CREATED, occurred_at=now)
    await publish(session, invoice, previous=InvoiceStatus.CREATED, occurred_at=now)
    await session.commit()

    assert len(await events_for(session, invoice.id)) == 1


async def test_a_second_publish_does_not_poison_the_transaction(session: AsyncSession):
    """The half that matters more than the row count.

    The point of ``ON CONFLICT DO NOTHING`` is not tidiness -- it is that the
    losing writer's status change still commits.
    """
    invoice = await make_invoice(session, ttl_minutes=-1)
    now = datetime.now(UTC)
    invoice.status = InvoiceStatus.EXPIRED.value
    await publish(session, invoice, previous=InvoiceStatus.CREATED, occurred_at=now)
    await publish(session, invoice, previous=InvoiceStatus.CREATED, occurred_at=now)

    await session.commit()

    await session.refresh(invoice)
    assert invoice.status == "expired"


async def test_rewriting_a_status_without_entering_it_publishes_nothing(
    session: AsyncSession,
):
    """Entering a status, not writing one.

    A refusal writes the status it refused with -- ``confirmed`` over
    ``confirmed`` -- and that is not a transition. Publishing on the write
    instead of on the entry would send the product an event every time somebody
    poked a settled invoice.
    """
    invoice = await make_invoice(session, status=InvoiceStatus.CONFIRMED)
    now = datetime.now(UTC)

    await publish(session, invoice, previous=InvoiceStatus.CONFIRMED, occurred_at=now)
    await session.commit()

    assert await events_for(session, invoice.id) == []


# ==========================================================================
# Delivery
# ==========================================================================


async def published(session: AsyncSession, **kwargs: object) -> OutboxEvent:
    """An invoice moved into a published status, with its event pending."""
    invoice = await make_invoice(session, ttl_minutes=-1)
    invoice.status = InvoiceStatus.EXPIRED.value
    await publish(
        session, invoice, previous=InvoiceStatus.CREATED, occurred_at=datetime.now(UTC)
    )
    await session.commit()
    event = await only_event(session, invoice.id)
    for field, value in kwargs.items():
        setattr(event, field, value)
    if kwargs:
        await session.commit()
    return event


async def test_an_accepted_event_is_delivered_once(session: AsyncSession):
    event = await published(session)
    transport = WebhookTransport(httpx.Response(200))

    async with httpx.AsyncClient(transport=transport) as client:
        taken = await deliver_once(session, settings(), client)

    await session.refresh(event)
    assert taken == 1
    assert transport.calls == 1
    assert event.delivery_state == "delivered"
    assert event.attempts == 1
    assert event.last_error is None


async def test_the_secret_travels_in_the_header_and_is_not_blank(session: AsyncSession):
    """The other half of the fail-closed pair.

    ``test_outbox`` proves a blank secret refuses to boot. On its own that
    would be satisfied by a service that sends no header at all, so the claim
    here is that the configured, non-empty value is what arrives.
    """
    await published(session)
    transport = WebhookTransport(httpx.Response(200))

    async with httpx.AsyncClient(transport=transport) as client:
        await deliver_once(session, settings(), client)

    sent = transport.requests[0]
    assert sent.headers["X-Payments-Secret"] == WEBHOOK_SECRET
    assert sent.headers["X-Payments-Secret"] != ""
    assert str(sent.url) == WEBHOOK_URL


async def test_the_body_on_the_wire_is_the_frozen_payload(session: AsyncSession):
    """Delivery sends what was published, not a fresh reading of the invoice.

    The invoice is moved on between publication and delivery. If the body were
    rebuilt at send time the product would be told about a state that never
    corresponded to this event.
    """
    event = await published(session)
    invoice = await session.get(Invoice, event.invoice_id)
    assert invoice is not None
    invoice.status = InvoiceStatus.CONFIRMED.value
    invoice.credited_amount_cents = 999
    await session.commit()

    transport = WebhookTransport(httpx.Response(200))
    async with httpx.AsyncClient(transport=transport) as client:
        await deliver_once(session, settings(), client)

    body = transport.requests[0].read().decode()
    assert '"status": "expired"' in body or '"status":"expired"' in body
    assert "999" not in body


@pytest.mark.parametrize("code", [204, 200, 299])
async def test_any_2xx_is_acceptance_whatever_the_body(session: AsyncSession, code: int):
    """The emptiness axis of the product's answer.

    A 204 with nothing in it has accepted the event as much as a 200 that
    echoes it. The status code is the contract; the body is never read.
    """
    event = await published(session)

    async with httpx.AsyncClient(transport=WebhookTransport(httpx.Response(code))) as client:
        await deliver_once(session, settings(), client)

    await session.refresh(event)
    assert event.delivery_state == "delivered"


@pytest.mark.parametrize("code", [400, 404, 409, 500, 503])
async def test_any_non_2xx_is_retried_including_4xx(session: AsyncSession, code: int):
    """No class of permanent failures, on purpose.

    A 404 from a product halfway through a deployment is indistinguishable from
    a 404 at a misconfigured path, and only one of the two is worth giving up
    on. Guessing wrong in the other direction loses a payment.
    """
    event = await published(session)

    async with httpx.AsyncClient(transport=WebhookTransport(httpx.Response(code))) as client:
        await deliver_once(session, settings(), client)

    await session.refresh(event)
    assert event.delivery_state == "pending"
    assert event.attempts == 1
    assert event.last_error == f"HTTP {code}"
    assert event.next_attempt_at > datetime.now(UTC)


async def test_an_unreachable_product_spends_an_attempt_and_is_named(
    session: AsyncSession,
):
    """The shortfall axis: no answer at all, rather than an unwelcome one."""
    event = await published(session)
    refused = httpx.ConnectError("nothing is listening")

    async with httpx.AsyncClient(transport=WebhookTransport(refused)) as client:
        await deliver_once(session, settings(), client)

    await session.refresh(event)
    assert event.delivery_state == "pending"
    assert event.attempts == 1
    assert "ConnectError" in (event.last_error or "")


async def test_the_budget_ends_in_failed_rather_than_silence(session: AsyncSession):
    """``failed`` arrives by the configured ceiling, and says why.

    Without ``last_error`` the state would be a dead end with no explanation
    and the operator's only recourse would be the log.
    """
    event = await published(session, attempts=2)
    conf = settings(WEBHOOK_MAX_ATTEMPTS=3)

    async with httpx.AsyncClient(transport=WebhookTransport(httpx.Response(500))) as client:
        await deliver_once(session, conf, client)

    await session.refresh(event)
    assert event.delivery_state == "failed"
    assert event.attempts == 3
    assert event.last_error == "HTTP 500"


async def test_a_failed_event_is_never_picked_up_again(session: AsyncSession):
    event = await published(session, delivery_state=DeliveryState.FAILED.value)

    claimed = await claim_due(session, settings(), datetime.now(UTC))

    assert event.id not in claimed


async def test_a_delivered_event_is_never_picked_up_again(session: AsyncSession):
    event = await published(session, delivery_state=DeliveryState.DELIVERED.value)

    claimed = await claim_due(session, settings(), datetime.now(UTC))

    assert event.id not in claimed


# ==========================================================================
# Claiming, restart, and the poison row
# ==========================================================================


async def test_a_pending_event_survives_a_restart(session: AsyncSession):
    """The done-when of P-16, built the only way it can honestly be built.

    The process is not restarted -- the row is. It was published by one unit of
    work, nothing delivered it, and a delivery loop that starts afterwards with
    its own client picks it up from the table. That is exactly what a restart
    leaves behind, and it needs no recovery pass to find.
    """
    event = await published(session)
    assert event.delivery_state == "pending"

    async with webhook_client() as client:
        taken = await deliver_once(session, settings(), client)

    await session.refresh(event)
    assert taken == 1
    assert event.delivery_state == "delivered"


async def test_a_claim_hides_the_row_from_a_second_loop(session: AsyncSession):
    """Two delivery loops must not both send one event.

    The same device the confirmations worker uses: the claim pushes the due
    time one lease into the future in the statement that takes the row, so a
    second claim finds nothing.
    """
    await published(session)
    now = datetime.now(UTC)

    first = await claim_due(session, settings(), now)
    second = await claim_due(session, settings(), now)

    assert len(first) == 1
    assert second == []


async def test_a_claim_raises_claims_and_not_the_budget(session: AsyncSession):
    """The two counters answer two different questions.

    ``attempts`` leads to ``failed`` and must only count finished deliveries;
    ``claims`` counts times the row was taken. A process killed mid-POST raises
    one and not the other, which is what makes ``claims - attempts`` the set of
    deliveries that started and never reported.
    """
    event = await published(session)

    await claim_due(session, settings(), datetime.now(UTC))

    await session.refresh(event)
    assert event.claims == 1
    assert event.attempts == 0


async def test_a_lease_that_expires_brings_the_row_back(session: AsyncSession):
    """A loop that died mid-delivery strands nothing forever."""
    conf = settings(WEBHOOK_LEASE_SECONDS=1.0)
    await published(session)
    now = datetime.now(UTC)

    taken = await claim_due(session, conf, now)
    again = await claim_due(session, conf, now + timedelta(seconds=2))

    assert taken == again


async def test_a_row_that_throws_cools_down_without_spending_the_budget(
    session: AsyncSession,
):
    """The poison row, and why it is not allowed to reach ``failed``.

    An exception from our own code is not an answer from the product. Spending
    the budget on it would mark a row ``failed`` while the product is perfectly
    healthy -- the direction of error this whole design refuses. So the budget
    is untouched and the row backs off on ``claims`` instead, which bounds both
    the log and the share of the batch it can occupy.
    """
    event = await published(session)

    # An exception that is not an HTTP error stands in for a defect in our own
    # code: something threw between taking the row and getting an answer. The
    # first shape tried here was an unserialisable payload, which Postgres
    # refuses to store -- a state the table cannot be in, and a test that built
    # it by hand would have documented the impossible.
    async with webhook_client(RuntimeError("a defect, not an answer")) as client:
        taken = await deliver_once(session, settings(), client)

    await session.refresh(event)
    assert taken == 1
    assert event.attempts == 0
    assert event.claims == 1
    assert event.delivery_state == "pending"
    assert event.next_attempt_at > datetime.now(UTC)


async def test_one_throwing_row_does_not_stop_the_rest_of_the_batch(
    session: AsyncSession,
):
    """The claim worth having about a guard.

    "No exception escaped" would pass on a guard that swallowed the whole
    batch. What must be true is that the other row was delivered anyway.

    Which of the two throws is deliberately not asserted: ``UPDATE ...
    RETURNING`` makes no promise about the order it hands rows back, and a test
    that pinned one would be pinning an accident of the driver rather than
    behaviour. The claim is about the batch, so it is stated over the batch.
    """
    first = await published(session)
    second = await published(session)

    async with webhook_client(
        RuntimeError("a defect, not an answer"), httpx.Response(200)
    ) as client:
        await deliver_once(session, settings(), client)

    await session.refresh(first)
    await session.refresh(second)
    rows = [first, second]
    assert sorted(row.delivery_state for row in rows) == ["delivered", "pending"]

    survivor = next(row for row in rows if row.delivery_state == "pending")
    # And the row that threw kept its budget: an exception from our own code is
    # not an answer from the product, so it must not push anything to failed.
    assert survivor.attempts == 0
    assert survivor.claims == 1


async def test_the_batch_is_bounded(session: AsyncSession):
    for _ in range(4):
        await published(session)

    claimed = await claim_due(session, settings(WORKER_BATCH_SIZE=2), datetime.now(UTC))

    assert len(claimed) == 2


async def test_nothing_due_is_not_an_error(session: AsyncSession):
    async with webhook_client() as client:
        taken = await deliver_once(session, settings(), client)

    assert taken == 0
    total = await session.scalar(select(func.count()).select_from(OutboxEvent))
    assert total == 0


# ==========================================================================
# Two real delivery loops -- the hole named in the H5 report
# ==========================================================================


async def test_two_delivery_loops_send_one_event_once(engine) -> None:
    """The claim H5 asserted about the design and did not test.

    H5 proved the claim in halves: that a second claim finds nothing, and that
    an expired lease brings a row back. Both ran sequentially in ONE session,
    which demonstrates the predicate and the lease but not the arbitration --
    two coroutines on one session serialise on it and race nothing.

    Here each loop gets its own session, as two processes would. The contract
    is narrow and worth stating exactly: the product receives the event ONCE,
    and the row ends ``delivered`` with a single spent attempt. At-least-once
    permits a duplicate after a crash; it does not excuse one produced merely
    by running two loops at the same time.

    A shared transport records both loops' requests, so a second POST cannot
    hide in the other client's history.

    What this cannot force is an overlap: the scheduler may run the two loops
    end to end, and then the second finds nothing because the row is already
    ``delivered`` rather than because it was locked. The assertion holds either
    way, and ``claims == 1`` is the part that distinguishes them -- a loop that
    took the row and then discovered it was already sent would have raised it.
    """
    from asyncio import gather

    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as setup:
        event = await published(setup)
        event_id = event.id

    transport = WebhookTransport(httpx.Response(200))
    conf = settings()

    async def loop() -> int:
        async with factory() as own, httpx.AsyncClient(transport=transport) as client:
            return await deliver_once(own, conf, client)

    taken = await gather(loop(), loop())

    async with factory() as check:
        row = await check.get(OutboxEvent, event_id)
        assert row is not None
        assert row.delivery_state == "delivered"
        assert row.attempts == 1
        # Claimed once: the loser's claim matched no rows at all, rather than
        # taking the row and finding it already sent.
        assert row.claims == 1

    assert transport.calls == 1
    assert sorted(taken) == [0, 1]
