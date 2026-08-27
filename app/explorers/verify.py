"""The entry point: one TXID in, one final verdict out.

Composes the three pieces in the order TOR section 7 requires, and the order
matters more than it looks:

1. **The format gate runs first, and no adapter is built until it passes.**
   Not merely "no request is sent" -- nothing that could send one is even
   constructed. TOR section 7 says a malformed TXID never reaches the explorer
   and never becomes an ``invoice_txid_attempts`` row, and this ordering is
   what makes that a property of the code rather than a promise in a comment.
2. The adapter is built from settings, which is where a misconfiguration
   surfaces (see either adapter's constructor).
3. The retry series runs on top and yields the final verdict.

A TXID shaped for another network is ``invalid_format`` and costs nothing.
Calling it ``wrong_network`` would spend a user attempt on a classification
the TOR does not define and on a trip to the explorer that never happened --
and the person who pays for that mistake three times has an invoice locked
against the money they may already have sent on the other chain.
"""

from __future__ import annotations

import asyncio

import httpx

from app.config import Settings
from app.domain.statuses import Verdict
from app.explorers.protocol import ExplorerResult
from app.explorers.registry import spec_for
from app.explorers.retry import RETRY_DELAYS, Sleeper, resolve
from app.explorers.txid_format import matches


async def verify_txid(
    *,
    network: str,
    txid: str,
    wallet_address: str,
    settings: Settings,
    client: httpx.AsyncClient,
    sleep: Sleeper = asyncio.sleep,
    delays: tuple[float, ...] = RETRY_DELAYS,
) -> ExplorerResult:
    """Verify one submitted TXID against one invoice.

    ``wallet_address`` is the invoice's stored snapshot, not
    ``settings.wallet_address_for(network)``: TOR section 4 requires
    verification to use the address the invoice was issued with, so that
    rotating the configured address cannot invalidate open invoices.

    Raises:
        UnknownNetworkError: if no adapter is registered for ``network``.
        ValueError: if ``wallet_address`` is blank, or settings carry a blank
            or malformed explorer credential.
    """
    spec = spec_for(network)

    if not matches(spec.txid_pattern, txid):
        return ExplorerResult(verdict=Verdict.INVALID_FORMAT)

    adapter = spec.build(settings, client)
    return await resolve(adapter, txid, wallet_address, sleep=sleep, delays=delays)
