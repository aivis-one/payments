"""The internal retry series (T-12).

This loop is the reason ``Verdict`` has no eighth member. TOR section 7 makes
the endpoint retry internally *before* deciding whether a ``not_found`` counts
as a real user attempt, so by the time a verdict leaves this function the
series is already over and ``not_found`` unambiguously means "spent". H1
recorded that this could not be observed anywhere yet; it is observable here.

**Only ``not_found`` is retried.** A fresh hash may simply not be indexed yet,
which is the case worth waiting on. ``api_error`` leaves immediately: TOR
section 7 explicitly allows either retrying or answering "resubmit the TXID"
without charging, the attempt is not spent either way, and infrastructure that
just failed is unlikely to recover inside the next second. ``matched``,
``wrong_address`` and ``wrong_network`` are content answers that no amount of
waiting will change.

**Why the delays are a parameter and not three literals.** ``RETRY_DELAYS``
alone determines both the pacing and the number of calls, so the two can never
disagree. Tests read the same constant instead of restating ``4``, which means
changing the series is one edit rather than one edit plus a hunt through
assertions.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Final

from app.domain.statuses import Verdict
from app.explorers.protocol import ExplorerAdapter, ExplorerResult

#: One call, then a retry after each delay: four calls, three sleeps, about
#: seven seconds of latency added to the original call (TOR section 7).
RETRY_DELAYS: Final[tuple[float, ...]] = (1.0, 2.0, 4.0)

#: Injected so tests can observe the pacing without waiting for it. A test
#: that really slept would take seven seconds and would still not prove the
#: delays were the intended ones.
Sleeper = Callable[[float], Awaitable[None]]


async def resolve(
    adapter: ExplorerAdapter,
    txid: str,
    wallet_address: str,
    *,
    sleep: Sleeper = asyncio.sleep,
    delays: tuple[float, ...] = RETRY_DELAYS,
) -> ExplorerResult:
    """Run the series and return the one verdict that leaves this layer.

    The last delay is never slept: the loop checks the answer it already has
    before waiting for another one. Sleeping after the final call would add
    four seconds to every exhausted lookup and buy nothing.
    """
    result = await adapter.lookup(txid, wallet_address)

    for delay in delays:
        if result.verdict is not Verdict.NOT_FOUND:
            return result
        await sleep(delay)
        result = await adapter.lookup(txid, wallet_address)

    return result
