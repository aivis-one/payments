"""The partial unique index, checked by behaviour rather than by reading DDL.

Reading the index definition would only prove that the string we wrote is the
string we wrote. These tests insert rows and see what the database does.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Invoice, InvoiceTxidAttempt

NETWORK = "USDT-ERC20"
TXID = "0x" + "a" * 64


async def _make_invoice(session: AsyncSession) -> Invoice:
    invoice = Invoice(
        id=uuid.uuid4(),
        product_ref="product-1",
        network=NETWORK,
        address="0xwallet",
        invoice_amount_cents=10_000,
        status="created",
        expires_at=datetime.now(UTC) + timedelta(minutes=60),
    )
    session.add(invoice)
    await session.commit()
    return invoice


def _attempt(invoice: Invoice, result_code: str, txid: str = TXID) -> InvoiceTxidAttempt:
    return InvoiceTxidAttempt(
        id=uuid.uuid4(),
        invoice_id=invoice.id,
        network=NETWORK,
        txid=txid,
        result_code=result_code,
    )


async def test_two_not_found_rows_with_the_same_txid_both_insert(session: AsyncSession):
    """Rejected attempts do not hold a TXID: the same wrong hash may repeat."""
    invoice = await _make_invoice(session)

    session.add(_attempt(invoice, "not_found"))
    session.add(_attempt(invoice, "not_found"))
    await session.commit()

    rows = (await session.execute(select(InvoiceTxidAttempt))).scalars().all()
    assert len(rows) == 2


async def test_second_matched_row_with_the_same_txid_fails(session: AsyncSession):
    """What must be unique is successful use, and only successful use."""
    invoice = await _make_invoice(session)

    session.add(_attempt(invoice, "matched"))
    await session.commit()

    session.add(_attempt(invoice, "matched"))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_matched_and_not_found_with_the_same_txid_coexist(session: AsyncSession):
    """The index only sees matched rows; the others are invisible to it."""
    invoice = await _make_invoice(session)

    session.add(_attempt(invoice, "not_found"))
    session.add(_attempt(invoice, "matched"))
    session.add(_attempt(invoice, "wrong_address"))
    await session.commit()

    rows = (await session.execute(select(InvoiceTxidAttempt))).scalars().all()
    assert len(rows) == 3


async def test_a_matched_txid_is_taken_across_invoices(session: AsyncSession):
    """Uniqueness is global, not per invoice -- that is the whole point.

    It is also why a stalled invoice keeps its TXID taken: the matched row
    stays, so the same transfer cannot be credited automatically on a second
    invoice while staff handles the first one by hand.
    """
    first = await _make_invoice(session)
    second = await _make_invoice(session)

    session.add(_attempt(first, "matched"))
    await session.commit()

    session.add(_attempt(second, "matched"))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


async def test_the_same_txid_on_another_network_is_a_different_row(session: AsyncSession):
    """The index is composite: (network, txid), not txid alone."""
    invoice = await _make_invoice(session)

    session.add(_attempt(invoice, "matched"))
    other = _attempt(invoice, "matched")
    other.network = "USDT-TRC20"
    session.add(other)
    await session.commit()

    rows = (await session.execute(select(InvoiceTxidAttempt))).scalars().all()
    assert len(rows) == 2


async def test_a_truncated_txid_is_a_different_string_to_the_database(session: AsyncSession):
    """Shortfall on the txid axis: the database compares strings, nothing more.

    Format validation is the regex layer's job (H2); the index does not do it.
    """
    invoice = await _make_invoice(session)

    session.add(_attempt(invoice, "matched"))
    session.add(_attempt(invoice, "matched", txid=TXID[:-1]))
    await session.commit()

    rows = (await session.execute(select(InvoiceTxidAttempt))).scalars().all()
    assert len(rows) == 2


async def test_an_empty_txid_is_storable(session: AsyncSession):
    """Emptiness on the txid axis: NOT NULL does not exclude the empty string.

    Nothing in the schema stops it, so the guarantee that it never arrives
    belongs to the format layer, not to the column.
    """
    invoice = await _make_invoice(session)

    session.add(_attempt(invoice, "not_found", txid=""))
    await session.commit()

    rows = (await session.execute(select(InvoiceTxidAttempt))).scalars().all()
    assert len(rows) == 1


async def test_invoice_defaults_are_applied(session: AsyncSession):
    """Paired positive check: the row exists and its counters are real values."""
    invoice = await _make_invoice(session)
    await session.refresh(invoice)

    assert invoice.attempts_used == 0
    assert invoice.status == "created"
    assert invoice.credited_amount_cents is None
    assert invoice.underpaid is None
    assert invoice.slot_frozen_at is None
    assert invoice.created_at is not None


async def test_worker_fields_are_present_and_nullable_where_they_must_be(
    session: AsyncSession,
):
    """The worker exists now, so the three columns do too.

    Until H4 this asserted the opposite -- that the three were **absent** --
    and it was right about what it measured: TOR section 4 kept them out of the
    first migration because a column nobody reads or writes is a promise the
    schema cannot keep. What overrode it is that the worker arrived, in its own
    revision, exactly as that note said it would.

    The assertion is not dropped, it is re-pointed at the property that matters
    now. ``next_check_at`` must stay nullable: NULL is its permanent meaning,
    "never looked at", because the API route that moves an invoice into
    ``awaiting_confirmations`` does not set it and is not meant to. A NOT NULL
    with a default here would make every migrated row look already scheduled.

    The paired half of the original is kept: absence, or presence, means
    nothing unless the columns that should be there are checked too.
    """
    columns = Invoice.__table__.columns

    assert {"confirmations_seen", "last_checked_at", "next_check_at"} <= set(columns.keys())
    assert {"status", "attempts_used", "expires_at", "slot_frozen_at"} <= set(columns.keys())

    assert columns["next_check_at"].nullable
    assert columns["last_checked_at"].nullable
    assert not columns["confirmations_seen"].nullable

    invoice = await _make_invoice(session)
    await session.refresh(invoice)

    assert invoice.confirmations_seen == 0
    assert invoice.last_checked_at is None
    assert invoice.next_check_at is None
