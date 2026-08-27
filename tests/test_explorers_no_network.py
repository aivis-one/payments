"""T-13: the suite makes no outgoing requests -- proved in two halves.

"No network calls were made" is worthless as evidence on its own. It passes on
a suite that ran nothing, on a suite whose assertions were all deleted, and on
a suite that silently skipped. So it is never asserted alone here.

The negative half is the ``network_attempts`` trap: three monkeypatched paths
that raise the moment anything tries to leave the machine.

The positive half is :class:`RecordingTransport`: what the HTTP layer was asked
for, and what it answered. A green run means *both* -- the mocked layer was
exercised and returned the frozen fixture, and nothing reached a socket.

The first test below is the one that keeps the trap honest. Without it, a
monkeypatch aimed at a method that no longer exists would silently arm nothing
and every "no calls were made" assertion in this directory would still pass.
"""

from __future__ import annotations

import pathlib
import socket

import httpx
import pytest

from app.domain.statuses import Verdict
from app.explorers.verify import verify_txid
from tests.conftest import NO_NETWORK_FAMILIES
from tests.explorers_support import (
    EVM_TXID,
    EVM_WALLET,
    TRON_TXID,
    TRON_WALLET,
    RecordingTransport,
    client_for,
    json_response,
    load,
    make_settings,
)

#: Every module in this family claims it reaches no explorer. The claim is
#: what arms the transport trap in tests/conftest.py -- see the docstring
#: there for why the trap is opt-in rather than always on.
pytestmark = pytest.mark.no_network


async def test_the_trap_actually_fires_on_a_real_transport(network_attempts: list[str]):
    """Proves the guard is armed rather than pointed at nothing.

    A client with httpx's default transport is exactly what a careless helper
    would build. It must not be able to reach the network from this directory,
    and if the monkeypatch ever stops matching httpx's internals, this is the
    test that goes red instead of the whole suite going quietly permissive.
    """
    async with httpx.AsyncClient() as client:
        with pytest.raises(AssertionError, match="outgoing network access"):
            await client.get("https://api.etherscan.io/v2/api")

    assert network_attempts == ["httpx.AsyncHTTPTransport"]


def test_the_socket_trap_is_armed_too(network_attempts: list[str]):
    """Anything bypassing httpx entirely still cannot get out."""
    with pytest.raises(AssertionError, match="outgoing network access"):
        socket.socket().connect(("example.invalid", 80))

    assert network_attempts == ["socket.connect"]


async def test_an_etherscan_lookup_is_served_entirely_from_a_fixture(
    network_attempts: list[str],
):
    """The pair, stated in one test.

    Positive: the transport was called once, with the URL and parameters the
    adapter is supposed to build, and the verdict is the one the frozen
    response implies. Negative: the trap recorded nothing.
    """
    transport = RecordingTransport(json_response(load("etherscan_erc20_single")))
    async with client_for(transport) as client:
        result = await verify_txid(
            network="USDT-ERC20",
            txid=EVM_TXID,
            wallet_address=EVM_WALLET,
            settings=make_settings(),
            client=client,
        )

    assert transport.calls == 1
    request = transport.requests[0]
    assert request.url.host == "api.etherscan.io"
    assert request.url.params["chainid"] == "1"
    assert request.url.params["txhash"] == EVM_TXID
    assert result.verdict is Verdict.MATCHED
    assert result.raw_amount == 100_000_000

    assert network_attempts == []


async def test_a_tronscan_lookup_is_served_entirely_from_a_fixture(network_attempts: list[str]):
    transport = RecordingTransport(json_response(load("tronscan_usdt_single")))
    async with client_for(transport) as client:
        result = await verify_txid(
            network="USDT-TRC20",
            txid=TRON_TXID,
            wallet_address=TRON_WALLET,
            settings=make_settings(),
            client=client,
        )

    assert transport.calls == 1
    assert transport.requests[0].url.host == "apilist.tronscan.org"
    assert transport.requests[0].url.params["hash"] == TRON_TXID
    assert result.verdict is Verdict.MATCHED

    assert network_attempts == []


async def test_a_full_exhausted_retry_series_never_touches_a_socket(
    network_attempts: list[str],
):
    """Four HTTP calls through the mock layer, zero through a socket.

    The heaviest path in this package: the series runs to exhaustion, so if
    any call fell through to a real transport this is where it would show.
    """
    responses = [json_response(load("etherscan_result_null")) for _ in range(4)]
    transport = RecordingTransport(*responses)
    delays_slept: list[float] = []

    async def sleep(delay: float) -> None:
        delays_slept.append(delay)

    async with client_for(transport) as client:
        result = await verify_txid(
            network="USDT-ERC20",
            txid=EVM_TXID,
            wallet_address=EVM_WALLET,
            settings=make_settings(),
            client=client,
            sleep=sleep,
        )

    assert transport.calls == 4
    assert delays_slept == [1.0, 2.0, 4.0]
    assert result.verdict is Verdict.NOT_FOUND

    assert network_attempts == []


async def test_the_recording_transport_refuses_to_invent_an_extra_answer(
    network_attempts: list[str],
):
    """The mock never improvises, so a stray call is a failure not a default.

    A transport that repeated its last response would let an off-by-one in the
    retry loop pass unnoticed.
    """
    transport = RecordingTransport(json_response(load("etherscan_result_null")))
    async with client_for(transport) as client:
        with pytest.raises(AssertionError, match="only 1 responses were prepared"):
            await verify_txid(
                network="USDT-ERC20",
                txid=EVM_TXID,
                wallet_address=EVM_WALLET,
                settings=make_settings(),
                client=client,
                sleep=_no_sleep,
            )

    assert network_attempts == []


async def _no_sleep(delay: float) -> None:
    """A sleeper for tests that only care that no socket opened."""
    return None


# --------------------------------------------------------------------------
# The price of the flat layout, paid here
# --------------------------------------------------------------------------


TESTS_DIR = pathlib.Path(__file__).parent


@pytest.mark.parametrize("family", NO_NETWORK_FAMILIES)
def test_every_module_of_a_no_network_family_declares_the_marker(family: str):
    """A directory could not be forgotten; a marker can.

    The trap is opt-in, so a module written without ``pytestmark`` would simply
    not be guarded -- and would pass, quietly, with the guarantee gone. That is
    what the flat layout costs, and this is where it is paid back.

    Parametrised over the families declared in ``tests/conftest.py`` rather
    than over a glob written here. H3 added a second family; hard-coding it
    would have made the obligation two copies, and the first person to add a
    third would have updated one of them.
    """
    modules = sorted(TESTS_DIR.glob(family))

    assert modules, f"the glob {family!r} matched nothing; the naming changed"
    missing = [m.name for m in modules if "pytest.mark.no_network" not in m.read_text()]
    assert missing == []


def test_the_database_tests_do_not_declare_the_marker():
    """The paired half, and the reason the trap is opt-in at all.

    asyncpg reaches Postgres through ``socket.connect``. If this module ever
    picked up the marker, all nine index tests would fail the moment the owner
    ran them against a real database -- and they skip on any machine without
    one, so the breakage would arrive on the server and nowhere earlier.
    """
    source = (TESTS_DIR / "test_partial_index.py").read_text()

    assert "no_network" not in source
