"""Shared fixtures.

Database tests skip when DATABASE_URL is unset, so the pure-domain suite runs
anywhere. In CI, PAYMENTS_TESTS_REQUIRE_DB turns that skip into a failure: a
green run must not be able to mean "the database tests silently did not run".
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.domain.events import InvoiceSnapshot
from app.domain.policy import Policy
from app.domain.statuses import InvoiceStatus

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
EXPIRES_AT = NOW + timedelta(minutes=60)

#: 6-decimal networks (USDT on Ethereum and Tron).
POLICY_6 = Policy(
    decimals=6,
    confirmations_required=12,
    max_txid_attempts=3,
    max_observation_window=timedelta(days=7),
)

#: 18-decimal network (Binance-Peg BSC-USD).
POLICY_18 = Policy(
    decimals=18,
    confirmations_required=15,
    max_txid_attempts=3,
    max_observation_window=timedelta(days=7),
)


#: The TXID holding the slot in every awaiting_confirmations snapshot built by
#: the helper below. Deliberately unlike any TXID the tests submit, so that a
#: test asking about a *foreign* TXID does not accidentally match the slot.
ACTIVE_TXID = "0x" + "f" * 64


def snapshot(
    status: InvoiceStatus,
    *,
    invoice_amount_cents: int = 10_000,
    attempts_used: int = 0,
    expires_at: datetime = EXPIRES_AT,
    slot_frozen_at: datetime | None = None,
    active_txid: str | None = None,
) -> InvoiceSnapshot:
    """Build a snapshot, defaulting the fields the awaiting invariants require.

    ``slot_frozen_at`` and ``active_txid`` are filled together because the
    transition that sets one sets the other.
    """
    if status is InvoiceStatus.AWAITING_CONFIRMATIONS:
        if slot_frozen_at is None:
            slot_frozen_at = NOW - timedelta(minutes=5)
        if active_txid is None:
            active_txid = ACTIVE_TXID
    return InvoiceSnapshot(
        status=status,
        invoice_amount_cents=invoice_amount_cents,
        attempts_used=attempts_used,
        expires_at=expires_at,
        slot_frozen_at=slot_frozen_at,
        active_txid=active_txid,
    )


#: The five statuses that refuse a TXID submission (TOR section 8).
REFUSING_STATUSES = [
    InvoiceStatus.AWAITING_CONFIRMATIONS,
    InvoiceStatus.ATTEMPTS_EXHAUSTED,
    InvoiceStatus.CONFIRMED,
    InvoiceStatus.EXPIRED,
    InvoiceStatus.STALLED,
]


def _database_url() -> str | None:
    return os.environ.get("DATABASE_URL")


@pytest.fixture(scope="session")
def database_url() -> str:
    """Return the test database URL, or skip (or fail, in CI) without one."""
    url = _database_url()
    if url is None:
        if os.environ.get("PAYMENTS_TESTS_REQUIRE_DB"):
            pytest.fail("PAYMENTS_TESTS_REQUIRE_DB is set but DATABASE_URL is not configured")
        pytest.skip("DATABASE_URL is not configured")
    return url


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    """Async engine against the test database."""
    eng = create_async_engine(database_url, future=True)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Session with the tables truncated before each test."""
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        from sqlalchemy import text

        await s.execute(text("truncate invoice_txid_attempts, invoices cascade"))
        await s.commit()
        yield s


#: Test families that must reach no explorer, and therefore must carry one of
#: the two markers below. Declared once, here, so that adding a family is one
#: line rather than a second copy of the rule.
NO_NETWORK_FAMILIES: tuple[str, ...] = ("test_explorers_*.py", "test_routes*.py")

#: The two markers, weakest first.
#:
#: ``no_explorer`` is the invariant the work actually needs: nothing in this
#: module may reach an explorer over HTTP. ``no_network`` adds "and nothing may
#: open a socket at all", which is strictly stronger and only available to a
#: module that does not use the database either.
#:
#: They were one marker until a module that legitimately talks to Postgres was
#: given it. asyncpg connects through ``socket.connect``, so the stronger trap
#: killed every database test in that file -- on the server, where the
#: database exists, and nowhere earlier. One name for two properties is what
#: made that possible; a module can hold the first and not the second.
NETWORK_MARKERS: tuple[str, ...] = ("no_explorer", "no_network")

#: Fixtures whose presence means the test genuinely needs a socket.
DB_FIXTURES: frozenset[str] = frozenset({"session", "engine", "database_url"})


@pytest.fixture(autouse=True)
def network_attempts(request: pytest.FixtureRequest) -> Iterator[list[str]]:
    """Arm a network trap for modules that declare one of NETWORK_MARKERS.

    Opt-in rather than always on, and the reason is the database tests: asyncpg
    reaches Postgres through ``socket.connect``, so a trap armed across the whole
    suite would kill every test that uses one the moment ``DATABASE_URL`` is
    configured. Those modules must keep the real socket, which is what
    ``no_explorer`` gives them: the HTTP transports are still trapped, so an
    explorer call is still impossible.

    Three paths are patched rather than one, because a single guard proves
    nothing: httpx's async transport is what the adapters use, the sync transport
    is what a careless helper reaches for, and ``socket.connect`` catches
    anything that bypasses httpx entirely.

    Yields the list of attempts, which stays empty in a healthy run. Tests assert
    on it directly -- an empty list is only evidence when it sits next to proof
    that the mocked layer was exercised.

    httpx is imported inside the armed branch so that a run of the pure-domain
    suite does not pay for an import it has no use for.
    """
    attempts: list[str] = []
    marks = {mark.name for mark in request.node.iter_markers()}
    declared = marks & set(NETWORK_MARKERS)
    if not declared:
        yield attempts
        return

    # The contradiction is refused loudly rather than resolved quietly. A test
    # that asks for both "no socket may open" and a database connection cannot
    # have what it asked for, and downgrading it silently would hand back a
    # guarantee weaker than the one its marker claims.
    needs_socket = DB_FIXTURES & set(request.fixturenames)
    if "no_network" in marks and needs_socket:
        pytest.fail(
            "no_network forbids every socket, but this test uses "
            f"{sorted(needs_socket)}, which reach the database over one. "
            "Use no_explorer: it still forbids reaching an explorer."
        )

    import socket

    import httpx

    def trap(name: str) -> Any:
        def _fail(*args: object, **kwargs: object) -> Any:
            attempts.append(name)
            raise AssertionError(f"outgoing network access attempted via {name}")

        return _fail

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            httpx.AsyncHTTPTransport, "handle_async_request", trap("httpx.AsyncHTTPTransport")
        )
        patch.setattr(httpx.HTTPTransport, "handle_request", trap("httpx.HTTPTransport"))
        if "no_network" in marks:
            patch.setattr(socket.socket, "connect", trap("socket.connect"))
        yield attempts
