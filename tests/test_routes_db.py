"""Route behaviour that only a real database can settle.

Everything here needs Postgres: the partial unique index, the savepoint around
a rejected INSERT, and the arbitration between two submissions of the same
hash. Nothing in this file is provable on a machine without a database, so it
skips there and runs on the server, where ``PAYMENTS_TESTS_REQUIRE_DB`` turns a
missing ``DATABASE_URL`` into a failure rather than a silent pass.

That split is deliberate rather than convenient. A stub session can be made to
raise ``IntegrityError`` on command, and a test built that way proves the
handler runs -- but not that the database would ever have raised it. The claim
worth having is that the index refuses the second row, and only the index can
demonstrate that.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import explorer_client, settings_dependency
from app.db import get_session
from app.main import app
from app.models import Invoice, InvoiceTxidAttempt
from tests.explorers_support import (
    EVM_TXID,
    EVM_WALLET,
    RecordingTransport,
    json_response,
    load,
    make_settings,
)

pytestmark = pytest.mark.no_network

TOKEN = "test-token-not-real"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
NETWORK = "USDT-ERC20"


def client_for(session: AsyncSession, *responses: httpx.Response) -> httpx.AsyncClient:
    transport = RecordingTransport(*responses)
    explorer = httpx.AsyncClient(transport=transport)
    settings = make_settings()
    app.dependency_overrides[settings_dependency] = lambda: settings
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[explorer_client] = lambda: explorer
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


async def make_invoice(
    session: AsyncSession, *, attempts_used: int = 0, ttl_minutes: int = 60
) -> Invoice:
    invoice = Invoice(
        id=uuid.uuid4(),
        product_ref="product-1",
        network=NETWORK,
        address=EVM_WALLET,
        invoice_amount_cents=10_000,
        status="created",
        attempts_used=attempts_used,
        expires_at=datetime.now(UTC) + timedelta(minutes=ttl_minutes),
    )
    session.add(invoice)
    await session.commit()
    return invoice


async def count_attempts(session: AsyncSession, invoice_id: uuid.UUID) -> int:
    total = await session.scalar(
        select(func.count())
        .select_from(InvoiceTxidAttempt)
        .where(InvoiceTxidAttempt.invoice_id == invoice_id)
    )
    return int(total or 0)


# ==========================================================================
# The ordinary paths, persisted
# ==========================================================================


async def test_creating_an_invoice_writes_a_row_that_can_be_read_back(session: AsyncSession):
    async with client_for(session) as http:
        created = await http.post(
            "/api/v1/invoices",
            json={"product_ref": "p-1", "network": NETWORK, "invoice_amount_cents": 4_200},
            headers=AUTH,
        )
        assert created.status_code == 201
        body = created.json()

        read = await http.get(f"/api/v1/invoices/{body['id']}", headers=AUTH)

    assert read.status_code == 200
    assert read.json()["address"] == body["address"] == EVM_WALLET
    assert read.json()["invoice_amount_cents"] == 4_200
    assert read.json()["attempts_remaining"] == 3


async def test_a_matched_txid_takes_the_slot_and_writes_one_attempt(session: AsyncSession):
    invoice = await make_invoice(session)

    async with client_for(session, json_response(load("etherscan_erc20_single"))) as http:
        response = await http.post(
            f"/api/v1/invoices/{invoice.id}/txid", json={"txid": EVM_TXID}, headers=AUTH
        )

    body = response.json()
    assert body["status"] == "awaiting_confirmations"
    assert body["result_code"] == "matched"
    assert body["attempts_used"] == 1
    assert body["attempts_remaining"] == 2
    assert await count_attempts(session, invoice.id) == 1

    await session.refresh(invoice)
    assert invoice.active_txid == EVM_TXID
    assert invoice.slot_frozen_at is not None


async def test_a_not_found_spends_an_attempt_and_leaves_the_invoice_open(session: AsyncSession):
    """Four explorer calls, one row, one attempt: the retry series is exhausted."""
    invoice = await make_invoice(session)
    misses = [json_response(load("etherscan_result_null")) for _ in range(4)]

    async with client_for(session, *misses) as http:
        response = await http.post(
            f"/api/v1/invoices/{invoice.id}/txid", json={"txid": EVM_TXID}, headers=AUTH
        )

    body = response.json()
    assert body["status"] == "created"
    assert body["result_code"] == "not_found"
    assert body["attempts_used"] == 1
    assert await count_attempts(session, invoice.id) == 1


async def test_the_last_attempt_closes_the_invoice(session: AsyncSession):
    invoice = await make_invoice(session, attempts_used=2)
    misses = [json_response(load("etherscan_result_null")) for _ in range(4)]

    async with client_for(session, *misses) as http:
        response = await http.post(
            f"/api/v1/invoices/{invoice.id}/txid", json={"txid": EVM_TXID}, headers=AUTH
        )

    assert response.json()["status"] == "attempts_exhausted"
    assert response.json()["attempts_remaining"] == 0

    await session.refresh(invoice)
    assert invoice.status == "attempts_exhausted"


async def test_a_foreign_token_transfer_is_not_found_and_still_costs_an_attempt(
    session: AsyncSession,
):
    """The legacy bug, seen from the route rather than from the adapter."""
    invoice = await make_invoice(session)
    misses = [json_response(load("etherscan_erc20_foreign_only")) for _ in range(4)]

    async with client_for(session, *misses) as http:
        response = await http.post(
            f"/api/v1/invoices/{invoice.id}/txid", json={"txid": EVM_TXID}, headers=AUTH
        )

    assert response.json()["result_code"] == "not_found"
    assert response.json()["attempts_used"] == 1


# ==========================================================================
# The unique index arbitrates
# ==========================================================================


async def test_resubmitting_the_held_txid_is_free_and_not_a_409(session: AsyncSession):
    """Same hash, same invoice, after the slot was taken. A double click.

    Answering ``slot_occupied`` would be the only one of the five refusal codes
    that is false here: the slot is not held by another TXID, it is held by
    this one.
    """
    invoice = await make_invoice(session)

    async with client_for(session, json_response(load("etherscan_erc20_single"))) as http:
        first = await http.post(
            f"/api/v1/invoices/{invoice.id}/txid", json={"txid": EVM_TXID}, headers=AUTH
        )
    assert first.json()["status"] == "awaiting_confirmations"

    async with client_for(session) as http:
        second = await http.post(
            f"/api/v1/invoices/{invoice.id}/txid", json={"txid": EVM_TXID}, headers=AUTH
        )

    assert second.status_code == 200
    assert second.json()["result_code"] == "matched"
    assert second.json()["attempts_used"] == 1
    assert await count_attempts(session, invoice.id) == 1


async def test_a_different_txid_against_the_held_slot_is_409(session: AsyncSession):
    """The pair to the replay above, and the reason both must exist."""
    invoice = await make_invoice(session)

    async with client_for(session, json_response(load("etherscan_erc20_single"))) as http:
        await http.post(
            f"/api/v1/invoices/{invoice.id}/txid", json={"txid": EVM_TXID}, headers=AUTH
        )

    async with client_for(session) as http:
        response = await http.post(
            f"/api/v1/invoices/{invoice.id}/txid",
            json={"txid": "0x" + "cd" * 32},
            headers=AUTH,
        )

    assert response.status_code == 409
    assert response.json() == {"error": "slot_occupied"}
    assert await count_attempts(session, invoice.id) == 1


async def test_a_txid_held_by_another_invoice_is_already_used(session: AsyncSession):
    """``already_used`` is produced here, not by any explorer.

    No adapter can know that a different invoice already holds this hash -- the
    fact lives in our own index. The route learns it from the ``IntegrityError``
    and re-reads the winning row to see whose it is.
    """
    first = await make_invoice(session)
    second = await make_invoice(session)

    async with client_for(session, json_response(load("etherscan_erc20_single"))) as http:
        await http.post(
            f"/api/v1/invoices/{first.id}/txid", json={"txid": EVM_TXID}, headers=AUTH
        )

    async with client_for(session, json_response(load("etherscan_erc20_single"))) as http:
        response = await http.post(
            f"/api/v1/invoices/{second.id}/txid", json={"txid": EVM_TXID}, headers=AUTH
        )

    assert response.status_code == 200
    assert response.json()["result_code"] == "already_used"
    assert response.json()["attempts_used"] == 1
    assert await count_attempts(session, second.id) == 1

    await session.refresh(second)
    assert second.active_txid is None
    assert second.status == "created"


async def test_a_rejected_insert_does_not_poison_the_transaction(session: AsyncSession):
    """The savepoint earns its place here.

    Without ``begin_nested`` the failed INSERT would abort the whole
    transaction, and the request could only be abandoned -- the
    ``already_used`` row that follows it could not be written at all.
    """
    first = await make_invoice(session)
    second = await make_invoice(session)

    async with client_for(session, json_response(load("etherscan_erc20_single"))) as http:
        await http.post(
            f"/api/v1/invoices/{first.id}/txid", json={"txid": EVM_TXID}, headers=AUTH
        )
    async with client_for(session, json_response(load("etherscan_erc20_single"))) as http:
        await http.post(
            f"/api/v1/invoices/{second.id}/txid", json={"txid": EVM_TXID}, headers=AUTH
        )

    rows = (
        await session.scalars(
            select(InvoiceTxidAttempt).where(InvoiceTxidAttempt.txid == EVM_TXID)
        )
    ).all()

    assert sorted(row.result_code for row in rows) == ["already_used", "matched"]


async def test_two_not_found_rows_for_one_hash_are_allowed(session: AsyncSession):
    """The index is partial, and this is what that means in practice.

    Only a *matched* row must be globally unique. The same wrong hash pasted
    against two invoices is two ordinary rows, and treating that as a conflict
    would refuse a user for somebody else's typo.
    """
    first = await make_invoice(session)
    second = await make_invoice(session)
    misses = [json_response(load("etherscan_result_null")) for _ in range(4)]

    for invoice in (first, second):
        async with client_for(session, *misses) as http:
            response = await http.post(
                f"/api/v1/invoices/{invoice.id}/txid", json={"txid": EVM_TXID}, headers=AUTH
            )
        assert response.json()["result_code"] == "not_found"

    assert await count_attempts(session, first.id) == 1
    assert await count_attempts(session, second.id) == 1


# ==========================================================================
# T-18: two requests at once
# ==========================================================================


async def test_a_concurrent_duplicate_never_answers_500(
    engine, database_url: str
) -> None:
    """Two real sessions, one hash, one invoice, submitted together.

    The contract is narrow and worth stating exactly: whatever the interleaving
    produces, it is 200 or 409 -- never a 500. One of the two INSERTs loses to
    the index, and losing must be an answer, not a crash.

    Each request gets its own session because that is the only way the race is
    real: two coroutines sharing one session would serialise on it and prove
    nothing.
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as setup:
        invoice = await make_invoice(setup)
        invoice_id = invoice.id

    async def submit() -> httpx.Response:
        async with (
            factory() as own,
            client_for(own, json_response(load("etherscan_erc20_single"))) as http,
        ):
            return await http.post(
                f"/api/v1/invoices/{invoice_id}/txid",
                json={"txid": EVM_TXID},
                headers=AUTH,
            )

    first, second = await asyncio.gather(submit(), submit(), return_exceptions=True)

    for outcome in (first, second):
        assert not isinstance(outcome, BaseException), outcome
        assert outcome.status_code in (200, 409), outcome.text

    async with factory() as check:
        matched = await check.scalar(
            select(func.count())
            .select_from(InvoiceTxidAttempt)
            .where(
                InvoiceTxidAttempt.txid == EVM_TXID,
                InvoiceTxidAttempt.result_code == "matched",
            )
        )
    assert matched == 1
