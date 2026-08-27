"""T-12: the internal retry series, and the H1 debt it closes.

H1 could state that "inside the window an attempt is not spent" but could not
check it: the branch did not exist anywhere, because the domain has no member
for a provisional ``not_found``. It exists here. What makes it observable is
that this loop is the only thing between the adapter and
:class:`~app.domain.events.TxidVerdict` -- so if a ``not_found`` from the
middle of a series ever left this function, the domain would spend an attempt
on it, and these tests are where that would show.

Nothing here goes near HTTP. The loop's whole contract is written in verdicts,
so driving it through an adapter would be testing the adapters a second time
and the loop only by accident.
"""

from __future__ import annotations

import pytest

from app.domain.statuses import Verdict
from app.explorers.protocol import ExplorerResult
from app.explorers.retry import RETRY_DELAYS, resolve
from tests.explorers_support import EVM_TXID, EVM_WALLET, FakeAdapter, RecordingSleeper

#: Every module in this family claims it reaches no explorer. The claim is
#: what arms the transport trap in tests/conftest.py -- see the docstring
#: there for why the trap is opt-in rather than always on.
pytestmark = pytest.mark.no_network

MAX_CALLS = len(RETRY_DELAYS) + 1

NOT_FOUND = ExplorerResult(verdict=Verdict.NOT_FOUND)
API_ERROR = ExplorerResult(verdict=Verdict.API_ERROR)
MATCHED = ExplorerResult(verdict=Verdict.MATCHED, raw_amount=100_000_000)
WRONG_ADDRESS = ExplorerResult(verdict=Verdict.WRONG_ADDRESS)


async def run(*results: ExplorerResult) -> tuple[ExplorerResult, FakeAdapter, RecordingSleeper]:
    adapter = FakeAdapter(*results)
    sleeper = RecordingSleeper()
    result = await resolve(adapter, EVM_TXID, EVM_WALLET, sleep=sleeper)
    return result, adapter, sleeper


# --------------------------------------------------------------------------
# The pacing the TOR fixes
# --------------------------------------------------------------------------


def test_the_series_is_one_call_plus_one_retry_per_delay():
    """Three delays, four calls, seven seconds added. TOR section 7.

    The call count is derived from the delays rather than written down twice,
    so the pacing and the budget cannot disagree with each other.
    """
    assert RETRY_DELAYS == (1.0, 2.0, 4.0)
    assert sum(RETRY_DELAYS) == 7.0
    assert MAX_CALLS == 4


async def test_a_not_found_inside_the_series_does_not_become_the_verdict():
    """The H1 debt, closed. Two misses then a hit -> matched.

    If the first ``not_found`` escaped, the domain would write an attempt row
    and spend one of three on a hash that was merely not indexed yet.
    """
    result, adapter, sleeper = await run(NOT_FOUND, NOT_FOUND, MATCHED)

    assert result.verdict is Verdict.MATCHED
    assert result.raw_amount == 100_000_000
    assert len(adapter.calls) == 3
    assert sleeper.delays == [1.0, 2.0]


async def test_an_exhausted_series_does_become_not_found():
    """The other half. After the last call it is a real, spent attempt."""
    result, adapter, sleeper = await run(*[NOT_FOUND] * MAX_CALLS)

    assert result.verdict is Verdict.NOT_FOUND
    assert len(adapter.calls) == MAX_CALLS
    assert sleeper.delays == list(RETRY_DELAYS)


async def test_the_final_delay_is_never_slept_after_the_last_answer():
    """The loop checks the answer it has before waiting for another one.

    Sleeping after the final call would add four seconds to every exhausted
    lookup and buy nothing, since there is no call left to make.
    """
    _, adapter, sleeper = await run(*[NOT_FOUND] * MAX_CALLS)

    assert len(sleeper.delays) == len(adapter.calls) - 1


async def test_a_match_on_the_first_call_costs_no_waiting_at_all():
    result, adapter, sleeper = await run(MATCHED)

    assert result.verdict is Verdict.MATCHED
    assert len(adapter.calls) == 1
    assert sleeper.delays == []


# --------------------------------------------------------------------------
# What is retried and what is not
# --------------------------------------------------------------------------


async def test_api_error_leaves_the_series_immediately():
    """TOR section 7 allows either shape; this one costs the user no latency.

    The attempt is not spent either way, and infrastructure that just failed
    is unlikely to recover within the second. Retrying would add seven seconds
    to a request that is going to tell the user "try again" regardless.
    """
    result, adapter, sleeper = await run(API_ERROR)

    assert result.verdict is Verdict.API_ERROR
    assert len(adapter.calls) == 1
    assert sleeper.delays == []


async def test_an_api_error_after_a_not_found_still_leaves_as_api_error():
    """The verdict that leaves is the last one seen, not the first.

    A series that starts with an unindexed hash and then hits a rate limit
    must not report ``not_found``: the user would be charged an attempt for
    our throttling.
    """
    result, adapter, _ = await run(NOT_FOUND, API_ERROR)

    assert result.verdict is Verdict.API_ERROR
    assert len(adapter.calls) == 2


@pytest.mark.parametrize("terminal", [MATCHED, WRONG_ADDRESS, API_ERROR])
async def test_every_non_not_found_verdict_ends_the_series(terminal: ExplorerResult):
    """Only ``not_found`` is provisional. Everything else is an answer."""
    result, adapter, sleeper = await run(terminal)

    assert result.verdict is terminal.verdict
    assert len(adapter.calls) == 1
    assert sleeper.delays == []


async def test_the_matched_payload_survives_the_series_intact():
    """The loop is a filter on verdicts, not a rewriter of results."""
    matched = ExplorerResult(
        verdict=Verdict.MATCHED, raw_amount=12_345, from_address="0xabc"
    )
    result, _, _ = await run(NOT_FOUND, matched)

    assert result == matched


# --------------------------------------------------------------------------
# The delays as a parameter
# --------------------------------------------------------------------------


@pytest.mark.parametrize("delays", [(), (1.0,), (1.0, 2.0), (0.5, 0.5, 0.5, 0.5)])
async def test_the_delay_tuple_alone_decides_how_many_calls_happen(delays: tuple[float, ...]):
    """Re-deciding the series is one edit, not an edit plus a hunt.

    Including the empty tuple: no retry at all is still one call, not zero.
    """
    adapter = FakeAdapter(*[NOT_FOUND] * (len(delays) + 1))
    sleeper = RecordingSleeper()

    result = await resolve(adapter, EVM_TXID, EVM_WALLET, sleep=sleeper, delays=delays)

    assert result.verdict is Verdict.NOT_FOUND
    assert len(adapter.calls) == len(delays) + 1
    assert sleeper.delays == list(delays)


async def test_every_call_asks_about_the_same_txid_and_wallet():
    """A retry is a repeat, not a variation."""
    _, adapter, _ = await run(*[NOT_FOUND] * MAX_CALLS)

    assert adapter.calls == [(EVM_TXID, EVM_WALLET)] * MAX_CALLS
