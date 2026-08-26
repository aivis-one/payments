"""Shared fixtures.

Database tests skip when DATABASE_URL is unset, so the pure-domain suite runs
anywhere. In CI, PAYMENTS_TESTS_REQUIRE_DB turns that skip into a failure: a
green run must not be able to mean "the database tests silently did not run".
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

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


def snapshot(
    status: InvoiceStatus,
    *,
    invoice_amount_cents: int = 10_000,
    attempts_used: int = 0,
    expires_at: datetime = EXPIRES_AT,
    slot_frozen_at: datetime | None = None,
) -> InvoiceSnapshot:
    """Build a snapshot, defaulting slot_frozen_at where the invariant needs it."""
    if slot_frozen_at is None and status is InvoiceStatus.AWAITING_CONFIRMATIONS:
        slot_frozen_at = NOW - timedelta(minutes=5)
    return InvoiceSnapshot(
        status=status,
        invoice_amount_cents=invoice_amount_cents,
        attempts_used=attempts_used,
        expires_at=expires_at,
        slot_frozen_at=slot_frozen_at,
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
