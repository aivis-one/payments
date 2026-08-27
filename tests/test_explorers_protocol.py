"""T-08: the adapter contract, and the one table that spells network names.

Two claims are worth checking mechanically rather than by reading. First, that
both adapters really do satisfy one interface -- a Protocol is only as good as
the classes that were checked against it. Second, that the network strings live
in exactly one place, because the whole plan for re-deciding the naming rests
on it.
"""

from __future__ import annotations

import pathlib

import pytest

from app.config import UnknownNetworkError
from app.domain.statuses import Verdict
from app.explorers.etherscan import EtherscanAdapter
from app.explorers.protocol import ExplorerAdapter, ExplorerResult
from app.explorers.registry import NETWORKS, spec_for
from app.explorers.tronscan import TronScanAdapter
from tests.explorers_support import (
    RecordingTransport,
    client_for,
    make_settings,
)

#: Every module in this family claims it reaches no explorer. The claim is
#: what arms the transport trap in tests/conftest.py -- see the docstring
#: there for why the trap is opt-in rather than always on.
pytestmark = pytest.mark.no_network

PACKAGE = pathlib.Path(__file__).resolve().parents[1] / "app" / "explorers"


# --------------------------------------------------------------------------
# One interface, two adapters
# --------------------------------------------------------------------------


async def test_both_adapters_satisfy_the_protocol():
    """Checked at runtime, not merely annotated.

    A Protocol that nothing was ever assigned to is a comment. Binding both
    concrete classes to the type here means a signature drifting apart from
    the interface fails mypy on this file.
    """
    transport = RecordingTransport()
    async with client_for(transport) as client:
        evm: ExplorerAdapter = EtherscanAdapter(
            client=client,
            api_url="https://api.etherscan.io/v2/api",
            api_key="k",
            contract_address="0xdAC17F958D2ee523a2206206994597C13D831ec7",
            chain_id=1,
        )
        tron: ExplorerAdapter = TronScanAdapter(
            client=client,
            api_url="https://apilist.tronscan.org/api",
            contract_address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
        )

    assert callable(evm.lookup)
    assert callable(tron.lookup)


@pytest.mark.parametrize("network", sorted(NETWORKS))
async def test_every_registered_network_builds_an_adapter(network: str):
    transport = RecordingTransport()
    async with client_for(transport) as client:
        adapter: ExplorerAdapter = spec_for(network).build(make_settings(), client)

    assert callable(adapter.lookup)


def test_the_protocol_module_knows_nothing_about_http_or_any_explorer():
    """The done-when, checked against the file rather than asserted in prose."""
    source = (PACKAGE / "protocol.py").read_text()
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    body = code.split('"""', 2)[-1]  # drop the module docstring

    for forbidden in ("httpx", "import http", "etherscan", "tronscan", "Etherscan", "TronScan"):
        assert forbidden not in body


# --------------------------------------------------------------------------
# The result contract
# --------------------------------------------------------------------------


def test_a_match_must_carry_an_amount():
    with pytest.raises(ValueError, match="raw_amount"):
        ExplorerResult(verdict=Verdict.MATCHED)


def test_a_match_cannot_carry_a_negative_amount():
    """An on-chain amount below zero does not exist."""
    with pytest.raises(ValueError, match="non-negative"):
        ExplorerResult(verdict=Verdict.MATCHED, raw_amount=-1)


def test_a_zero_amount_match_is_allowed():
    """A Transfer of zero is legal on chain, so the test is honest.

    ``raw_to_cents`` accepts it and the domain already confirms a zero-amount
    invoice, so nothing downstream needs a lower bound either.
    """
    assert ExplorerResult(verdict=Verdict.MATCHED, raw_amount=0).raw_amount == 0


@pytest.mark.parametrize(
    "verdict",
    [Verdict.NOT_FOUND, Verdict.WRONG_ADDRESS, Verdict.API_ERROR, Verdict.INVALID_FORMAT],
)
def test_only_a_match_may_carry_an_amount(verdict: Verdict):
    """An amount travelling with a non-match would eventually be read."""
    with pytest.raises(ValueError, match="raw_amount"):
        ExplorerResult(verdict=verdict, raw_amount=1)


@pytest.mark.parametrize("verdict", [Verdict.API_ERROR, Verdict.INVALID_FORMAT])
def test_a_verdict_that_parsed_nothing_may_not_name_a_sender(verdict: Verdict):
    """Neither of these ever saw a transfer, so a sender would be invented."""
    with pytest.raises(ValueError, match="from_address"):
        ExplorerResult(verdict=verdict, from_address="0xabc")


def test_a_wrong_address_may_name_a_sender():
    """TOR section 4: ``from_address`` is filled once the attempt was parsed.

    It is the Transfer's sender, not the account that submitted the
    transaction -- ``invoice_txid_attempts.from_address`` is about who paid.
    """
    result = ExplorerResult(verdict=Verdict.WRONG_ADDRESS, from_address="0xabc")

    assert result.from_address == "0xabc"


def test_the_result_is_frozen():
    """Nothing between the adapter and the domain gets to edit a verdict."""
    result = ExplorerResult(verdict=Verdict.NOT_FOUND)

    with pytest.raises(AttributeError):
        result.verdict = Verdict.MATCHED  # type: ignore[misc]


# --------------------------------------------------------------------------
# The one table of names
# --------------------------------------------------------------------------


def test_the_service_uses_the_prefixed_network_names():
    """What the tree holds today, so a change of mind is visible as a diff.

    The consuming product uses bare ``TRC20`` / ``ERC20`` / ``BEP20`` plus a
    fourth network, ``PoS``. The naming decision is the owner's and is open;
    this test pins the current answer rather than endorsing it.
    """
    assert sorted(NETWORKS) == ["USDT-BSC20", "USDT-ERC20", "USDT-TRC20"]


def test_each_network_name_appears_exactly_once_in_the_package():
    """The property the cheap-reversal plan depends on.

    If a name leaks into an adapter, the loop or a helper, renaming a network
    stops being one line per network and the isolation argument stops being
    true. Checked by counting occurrences across every module in the package.
    """
    counts = {name: 0 for name in NETWORKS}
    for path in sorted(PACKAGE.glob("*.py")):
        text = path.read_text()
        for name in counts:
            counts[name] += text.count(f'"{name}"')

    assert counts == {name: 1 for name in NETWORKS}


@pytest.mark.parametrize("network", ["", "TRC20", "ERC20", "BEP20", "PoS", "usdt-erc20", "BTC"])
def test_an_unregistered_network_raises_rather_than_returning_a_verdict(network: str):
    """Including the product's four names, none of which are ours today.

    Raised, not returned: "this service has no adapter for that string" is a
    configuration failure, and giving it a user-facing result code would let
    a deployment mismatch look like a payment outcome.
    """
    with pytest.raises(UnknownNetworkError):
        spec_for(network)
