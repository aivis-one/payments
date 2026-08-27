"""T-11: the TXID format gate, and that nothing reaches the network before it.

The done-when is not "a malformed TXID returns invalid_format" -- that much a
regex anywhere would give. It is that the network call *does not happen*, which
is a claim about ordering and can only be checked by watching the transport.
Every test here therefore asserts the verdict and the call count together.
"""

from __future__ import annotations

import pytest

from app.config import UnknownNetworkError
from app.domain.statuses import Verdict
from app.explorers.registry import NETWORKS
from app.explorers.txid_format import EVM_TXID, TRON_TXID, matches
from app.explorers.verify import verify_txid
from tests.explorers_support import (
    EVM_WALLET,
    TRON_WALLET,
    RecordingTransport,
    client_for,
    make_settings,
)

#: Every module in this family claims it reaches no explorer. The claim is
#: what arms the transport trap in tests/conftest.py -- see the docstring
#: there for why the trap is opt-in rather than always on.
pytestmark = pytest.mark.no_network

EVM_NETWORKS = ["USDT-ERC20", "USDT-BSC20"]

GOOD_EVM = "0x" + "ab" * 32
GOOD_TRON = "a2" * 32


async def gate(network: str, txid: str, wallet: str) -> tuple[Verdict, int]:
    """Run the whole pipeline with a transport that has nothing to give.

    A transport with an empty queue is the sharpest possible probe: if the
    format gate lets anything through, the request raises inside the handler
    instead of quietly returning a plausible verdict.
    """
    transport = RecordingTransport()
    async with client_for(transport) as client:
        result = await verify_txid(
            network=network,
            txid=txid,
            wallet_address=wallet,
            settings=make_settings(),
            client=client,
        )
    return result.verdict, transport.calls


# --------------------------------------------------------------------------
# The shapes themselves
# --------------------------------------------------------------------------


@pytest.mark.parametrize("network", EVM_NETWORKS)
def test_evm_networks_share_one_pattern_object(network: str):
    """Not merely equal patterns -- the same object.

    ERC20 and BSC20 cannot be told apart by format. Binding them to one shared
    pattern says so in the code, so that a later attempt to distinguish them
    by regex has to delete this test first.
    """
    assert NETWORKS[network].txid_pattern is EVM_TXID


def test_tron_has_its_own_pattern():
    assert NETWORKS["USDT-TRC20"].txid_pattern is TRON_TXID


@pytest.mark.parametrize(
    "txid",
    [
        "0x" + "ab" * 32,
        "0x" + "AB" * 32,  # upper-case hex is still hex
        "0x" + "0123456789abcdef" * 4,
    ],
)
def test_evm_pattern_accepts_a_prefixed_32_byte_hash(txid: str):
    assert matches(EVM_TXID, txid)


@pytest.mark.parametrize(
    "txid",
    [
        "",
        "   ",
        "0x" + "ab" * 31 + "a",  # 63 hex digits
        "0x" + "ab" * 32 + "a",  # 65
        "ab" * 32,  # no prefix: this is the TRON shape
        "0x" + "zz" * 32,  # not hex
        " 0x" + "ab" * 32,  # leading space
        "0x" + "ab" * 32 + " ",  # trailing space
    ],
)
def test_evm_pattern_rejects_everything_else(txid: str):
    assert not matches(EVM_TXID, txid)


def test_evm_pattern_rejects_a_trailing_newline():
    """The reason ``fullmatch`` is used instead of an anchored ``match``.

    ``$`` in Python also matches before a final newline, so an anchored pattern
    would accept a hash copied straight out of a terminal, and the trailing
    byte would travel all the way into the explorer URL and the database.
    """
    assert not matches(EVM_TXID, "0x" + "ab" * 32 + "\n")


@pytest.mark.parametrize("txid", ["", "   ", "a2" * 31, "0x" + "a2" * 32, "g" * 64])
def test_tron_pattern_rejects_everything_but_bare_64_hex(txid: str):
    assert not matches(TRON_TXID, txid)


# --------------------------------------------------------------------------
# The gate in place: no network call before the shape is right
# --------------------------------------------------------------------------


@pytest.mark.parametrize("network", EVM_NETWORKS)
@pytest.mark.parametrize("txid", ["", "   ", "0x" + "ab" * 31, "nonsense"])
async def test_a_malformed_evm_txid_never_reaches_the_explorer(network: str, txid: str):
    verdict, calls = await gate(network, txid, EVM_WALLET)

    assert verdict is Verdict.INVALID_FORMAT
    assert calls == 0


@pytest.mark.parametrize("txid", ["", "   ", "a2" * 31, "0x" + "a2" * 32])
async def test_a_malformed_tron_txid_never_reaches_the_explorer(txid: str):
    verdict, calls = await gate("USDT-TRC20", txid, TRON_WALLET)

    assert verdict is Verdict.INVALID_FORMAT
    assert calls == 0


@pytest.mark.parametrize("network", EVM_NETWORKS)
async def test_a_tron_shaped_hash_on_an_evm_invoice_is_free(network: str):
    """The owner's ruling on Q1, and it is about money.

    ``invalid_format`` costs the user nothing. Classifying it as a spent
    attempt instead would create a row and burn one of three. TOR section 7
    charges only for TXIDs that reached the explorer and got a content answer,
    and this one never left the process. Somebody who picks the wrong network
    three times in a UI would otherwise end up with a locked invoice and,
    quite possibly, money already sent on the other chain.
    """
    verdict, calls = await gate(network, GOOD_TRON, EVM_WALLET)

    assert verdict is Verdict.INVALID_FORMAT
    assert calls == 0


async def test_an_evm_shaped_hash_on_a_tron_invoice_is_free():
    verdict, calls = await gate("USDT-TRC20", GOOD_EVM, TRON_WALLET)

    assert verdict is Verdict.INVALID_FORMAT
    assert calls == 0


async def test_an_unknown_network_is_refused_before_the_format_is_looked_at():
    """Including the product's ``PoS`` and ``BEP20``.

    Raised rather than returned: there is no verdict for "this service has no
    adapter for that string", and inventing one would put a configuration
    failure into the user-facing result codes.
    """
    with pytest.raises(UnknownNetworkError):
        await gate("PoS", GOOD_EVM, EVM_WALLET)

    with pytest.raises(UnknownNetworkError):
        await gate("BEP20", GOOD_EVM, EVM_WALLET)


@pytest.mark.parametrize("network", sorted(NETWORKS))
async def test_every_registered_network_lets_its_own_shape_through(network: str):
    """Parametrised off the registry, not off literals.

    If the owner renames the networks, this test follows the rename instead of
    failing on it -- which is the whole point of keeping the names in one
    table.
    """
    txid = GOOD_TRON if network == "USDT-TRC20" else GOOD_EVM
    wallet = TRON_WALLET if network == "USDT-TRC20" else EVM_WALLET

    transport = RecordingTransport()
    async with client_for(transport) as client:
        with pytest.raises(AssertionError, match="only 0 responses"):
            await verify_txid(
                network=network,
                txid=txid,
                wallet_address=wallet,
                settings=make_settings(),
                client=client,
            )

    # It got past the gate and tried to call: exactly what a well-formed TXID
    # should do, and the opposite of every case above.
    assert transport.calls == 1
