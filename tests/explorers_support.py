"""Helpers for the explorer tests. Not collected -- the name is not ``test_*``.

Deliberately a flat module rather than a package: the tree keeps its tests flat,
and a subdirectory would also have carried a second ``conftest.py`` whose autouse
fixtures apply by *location*. A guarantee that holds because of where a file
sits is invisible from inside the file that relies on it.

So the network guarantee is split in two on purpose:

* the trap itself lives in ``tests/conftest.py`` and arms only for modules that
  declare ``pytestmark = pytest.mark.no_network`` -- the claim is visible in the
  file making it, and any future module (H3's routes, H4's worker) opts in with
  one line;
* the evidence that the mocked layer was actually exercised lives here, in
  :class:`RecordingTransport`.

Both halves are needed. "No outgoing requests were made" passes trivially on a
suite that executed nothing, so it is never asserted alone: every test in
``test_explorers_no_network.py`` pairs it with what the transport was asked for
and what it returned.

The frozen responses in ``fixtures/`` were reconstructed from published API
documentation and never captured from a live probe. What that does and does not
vouch for -- and which two files to replace first when real captures arrive --
is stated in ``tests/fixtures/PROVENANCE.md`` and only there.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import httpx

from app.config import Settings
from app.explorers.protocol import ExplorerResult

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

# -- Addresses used across the explorer tests -------------------------------
# EVM values are written in EIP-55 mixed case exactly as TOR section 10 and
# app/config.py store them; the fixtures carry them lower-cased the way a log
# topic does. Every comparison in these tests therefore crosses that boundary
# rather than sidestepping it.

EVM_WALLET = "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
EVM_SENDER = "0x292f04a44506c2FD49bAc032e1ca148C35a478C8"
EVM_OTHER = "0xAb6960A6511FF18ED8B8c012cb91C7F637947Fc0"
USDT_ERC20 = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
USDT_BSC20 = "0x55d398326f99059fF775485246999027B3197955"
FOREIGN_ERC20 = "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984"

TRON_WALLET = "TWbdVwjHTNn2PXDPbtSNvvESDd8PpApFmX"
TRON_SENDER = "TLa2f6VPqDgRE67v1736s7bJ8Ray5wYjU7"
TRON_OTHER = "TAWE8B9DXDgTmfr3bhFsjZ4U8Jx4nFTTTT"
USDT_TRC20 = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
FOREIGN_TRC20 = "TUpMhErZL2fhh4sVNULAbNKLokS4GjC1F4"

EVM_TXID = "0x" + "ab" * 32
TRON_TXID = "a2" * 32


def load(name: str) -> Any:
    """Load one frozen explorer response. See ``fixtures/PROVENANCE.md``."""
    return json.loads((FIXTURES / f"{name}.json").read_text())


class RecordingTransport(httpx.MockTransport):
    """Serves a queue of prepared responses and remembers every request.

    Structural, not promised: :class:`httpx.MockTransport` never opens a
    socket, so a test wired to one cannot reach the network even if the trap
    above were removed.
    """

    def __init__(self, *responses: httpx.Response) -> None:
        self.requests: list[httpx.Request] = []
        self._queue = list(responses)
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._queue:
            raise AssertionError(
                f"the adapter made {len(self.requests)} calls but only "
                f"{len(self.requests) - 1} responses were prepared"
            )
        return self._queue.pop(0)

    @property
    def calls(self) -> int:
        return len(self.requests)


def json_response(payload: object, status_code: int = 200) -> httpx.Response:
    """A prepared JSON response."""
    return httpx.Response(status_code=status_code, json=payload)


def raw_response(content: bytes, status_code: int = 200) -> httpx.Response:
    """A prepared response with a body that may not be JSON at all."""
    return httpx.Response(status_code=status_code, content=content)


def client_for(transport: httpx.MockTransport) -> httpx.AsyncClient:
    """An httpx client wired to a mock transport and to nothing else."""
    return httpx.AsyncClient(transport=transport)


def make_settings(**overrides: object) -> Settings:
    """Settings with every mandatory field supplied explicitly.

    Keyword arguments outrank both the environment and any ``.env``, so a test
    that pins a value gets that value regardless of what CI exports.
    """
    values: dict[str, object] = {
        "DATABASE_URL": "postgresql+asyncpg://payments:payments@localhost:5432/payments",
        # Mandatory since H3: Settings refuses to build without a token, and
        # refuses wallet addresses that are not shaped like real ones.
        "SERVICE_TOKEN": "test-token-not-real",
        "WALLET_ADDRESS_USDT_TRC20": TRON_WALLET,
        "WALLET_ADDRESS_USDT_ERC20": EVM_WALLET,
        "WALLET_ADDRESS_USDT_BSC20": EVM_WALLET,
        "ETHERSCAN_API_KEY": "test-key-not-real",
        "TRONSCAN_API_KEY": "test-key-not-real",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


class FakeAdapter:
    """An adapter that answers from a queue and counts its calls.

    Used by the retry tests: the loop's contract is stated entirely in terms of
    verdicts, so exercising it through HTTP would test the adapters again and
    the loop only incidentally.
    """

    def __init__(self, *results: ExplorerResult) -> None:
        self._queue = list(results)
        self.calls: list[tuple[str, str]] = []

    async def lookup(self, txid: str, wallet_address: str) -> ExplorerResult:
        self.calls.append((txid, wallet_address))
        if not self._queue:
            raise AssertionError(
                f"the loop made {len(self.calls)} calls but only "
                f"{len(self.calls) - 1} results were queued"
            )
        return self._queue.pop(0)


class RecordingSleeper:
    """Records the delays it was asked to wait for, without waiting."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
