"""Network name -> TXID shape and adapter. The only place names are spelled.

Each network string appears exactly **once** in this package, here. That is
not tidiness: the service and its consuming product disagree about these
strings today. The service uses ``USDT-TRC20`` / ``USDT-ERC20`` /
``USDT-BSC20`` (TOR sections 4 and 8, and ``app/config.py``); the product uses
bare ``TRC20`` / ``ERC20`` / ``BEP20`` plus a fourth network, ``PoS``, which
has no adapter and no config here. The naming decision is the owner's and has
not been made. Confining the names to one table makes re-deciding it cost one
line per network in this file plus the matching keys in ``app/config.py`` --
and nothing in the adapters, the loop, or the tests, which read
``NETWORKS.keys()`` rather than restating literals.

Adding a fourth network is a row here plus config, never a model migration --
TOR section 12.

``UnknownNetworkError`` is reused from :mod:`app.config` rather than defined
again: an unconfigured network is one boundary, not two, and it already lives
at the config edge because ``policy_for`` raises it there.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import httpx

from app.config import Settings, UnknownNetworkError
from app.explorers.etherscan import EtherscanAdapter
from app.explorers.protocol import ExplorerAdapter
from app.explorers.tronscan import TronScanAdapter
from app.explorers.txid_format import EVM_TXID, TRON_TXID

AdapterBuilder = Callable[[Settings, httpx.AsyncClient], ExplorerAdapter]


@dataclass(frozen=True, slots=True)
class NetworkSpec:
    """Everything about one network that is not the network's name."""

    txid_pattern: re.Pattern[str]
    build: AdapterBuilder


def _etherscan(chain_id: int, contract: Callable[[Settings], str]) -> AdapterBuilder:
    """Bind one EVM chain to its chain id and contract setting."""

    def build(settings: Settings, client: httpx.AsyncClient) -> ExplorerAdapter:
        return EtherscanAdapter(
            client=client,
            api_url=settings.ETHERSCAN_API_URL,
            api_key=settings.ETHERSCAN_API_KEY,
            contract_address=contract(settings),
            chain_id=chain_id,
        )

    return build


def _tronscan(contract: Callable[[Settings], str]) -> AdapterBuilder:
    def build(settings: Settings, client: httpx.AsyncClient) -> ExplorerAdapter:
        return TronScanAdapter(
            client=client,
            api_url=settings.TRONSCAN_API_URL,
            contract_address=contract(settings),
        )

    return build


NETWORKS: Final[dict[str, NetworkSpec]] = {
    "USDT-TRC20": NetworkSpec(
        txid_pattern=TRON_TXID,
        build=_tronscan(lambda settings: settings.USDT_CONTRACT_TRC20),
    ),
    "USDT-ERC20": NetworkSpec(
        txid_pattern=EVM_TXID,
        build=_etherscan(1, lambda settings: settings.USDT_CONTRACT_ERC20),
    ),
    "USDT-BSC20": NetworkSpec(
        txid_pattern=EVM_TXID,
        build=_etherscan(56, lambda settings: settings.USDT_CONTRACT_BSC20),
    ),
}


def spec_for(network: str) -> NetworkSpec:
    """Return the spec for ``network``.

    Raises:
        UnknownNetworkError: for any string this service has no adapter for --
            including the product's ``PoS``, and including ``BEP20`` for as
            long as the naming question above is open.
    """
    try:
        return NETWORKS[network]
    except KeyError as exc:
        raise UnknownNetworkError(network) from exc
