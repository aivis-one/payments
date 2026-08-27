"""Worker pieces that no database is needed to settle.

The schedule, the ceiling constant, and what the two adapters make of a
transaction they are asked to re-examine. Everything about claiming, writing
and racing lives in ``test_worker_db.py``, where a real Postgres can answer.

This module holds ``no_network``, the stronger of the two markers: it touches
no database, so it can promise that nothing opens a socket at all.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from app.config import Settings
from app.domain.statuses import Verdict
from app.explorers.etherscan import EtherscanAdapter
from app.explorers.protocol import ExplorerObservation, ExplorerResult
from app.explorers.tronscan import TronScanAdapter
from app.worker import BIGINT_MAX, poll_interval
from tests.explorers_support import (
    EVM_TXID,
    EVM_WALLET,
    TRON_TXID,
    TRON_WALLET,
    USDT_ERC20,
    USDT_TRC20,
    RecordingTransport,
    json_response,
    load,
    make_settings,
)

pytestmark = pytest.mark.no_network

ETHERSCAN_URL = "https://api.etherscan.io/v2/api"
TRONSCAN_URL = "https://apilist.tronscanapi.com/api"

#: The receipt fixtures sit at block 0xcf2427, the head fixture at 0xcf2500.
EXPECTED_DEPTH = 0xCF2500 - 0xCF2427


def evm_adapter(client: httpx.AsyncClient) -> EtherscanAdapter:
    return EtherscanAdapter(
        client=client,
        api_url=ETHERSCAN_URL,
        api_key="test-key-not-real",
        contract_address=USDT_ERC20,
        chain_id=1,
    )


def tron_adapter(client: httpx.AsyncClient) -> TronScanAdapter:
    return TronScanAdapter(
        client=client,
        api_url=TRONSCAN_URL,
        api_key="test-key-not-real",
        contract_address=USDT_TRC20,
    )


# ==========================================================================
# The polling schedule
# ==========================================================================


def test_a_fresh_slot_is_polled_at_the_floor():
    """Confirmations accrue in minutes, so the first ten are the fast window."""
    settings = make_settings()

    assert poll_interval(timedelta(0), settings) == timedelta(seconds=30)
    assert poll_interval(timedelta(minutes=9), settings) == timedelta(seconds=30)


def test_the_interval_doubles_every_ten_minutes():
    settings = make_settings()

    assert poll_interval(timedelta(minutes=10), settings) == timedelta(seconds=60)
    assert poll_interval(timedelta(minutes=20), settings) == timedelta(seconds=120)
    assert poll_interval(timedelta(minutes=30), settings) == timedelta(seconds=240)


def test_the_interval_stops_at_the_ceiling():
    """Reached after about an hour, and it stays there for the whole window."""
    settings = make_settings()

    assert poll_interval(timedelta(hours=2), settings) == timedelta(seconds=3600)
    assert poll_interval(timedelta(days=7), settings) == timedelta(seconds=3600)


def test_a_week_old_slot_does_not_compute_an_astronomical_number_first():
    """The cap is applied to the exponent, not only to the result.

    Doubling for seven days is 2**1008 before clamping. Python would compute it
    rather than overflow, but building a three-hundred-digit integer to throw it
    away is the kind of thing that is fine until somebody passes a year.
    """
    settings = make_settings()

    assert poll_interval(timedelta(days=365), settings) == timedelta(seconds=3600)


@pytest.mark.parametrize("age", [timedelta(0), timedelta(seconds=-5), timedelta(days=-1)])
def test_a_non_positive_age_is_the_floor_not_an_error(age: timedelta):
    """A clock that went backwards is not a reason to stop polling."""
    settings = make_settings()

    assert poll_interval(age, settings) == timedelta(seconds=30)


def test_the_schedule_is_far_cheaper_than_polling_every_minute():
    """The number the whole backoff exists for -- measured, not claimed.

    Seven days of minute polling is 10,080 calls per stuck invoice against a
    key we now pay for. This schedule costs 208. At the gate I called that two
    orders of magnitude; it is 48 times, which is one and a half. The saving is
    real and the claim was rounded in my favour, so the test asserts what the
    arithmetic actually produces.
    """
    settings = make_settings()
    elapsed = timedelta(0)
    calls = 0
    while elapsed < timedelta(days=7):
        elapsed += poll_interval(elapsed, settings)
        calls += 1

    assert calls == 208
    assert 10_080 / calls > 40


# ==========================================================================
# The ceiling
# ==========================================================================


def test_the_ceiling_is_the_width_of_the_column():
    """Not a round number picked for readability: it is what bigint holds."""
    assert BIGINT_MAX == 9_223_372_036_854_775_807


def test_a_real_payment_cannot_reach_the_ceiling():
    """Why the guard is about corrupt responses, not about accounting.

    BSC-USD has the widest raw values in the service at 18 decimals, and its
    entire supply is about 1e9 tokens. Crossing the ceiling would take seven
    orders of magnitude more than exists, and the emitting contract is checked,
    so no genuine transfer can produce it.
    """
    supply_raw = 10**9 * 10**18
    cents_if_all_of_it_arrived = supply_raw // 10 ** (18 - 2)

    assert cents_if_all_of_it_arrived < BIGINT_MAX / 10**6


# ==========================================================================
# Observation: what the adapters report
# ==========================================================================


async def test_the_evm_adapter_subtracts_the_head_from_the_block():
    transport = RecordingTransport(
        json_response(load("etherscan_erc20_single")),
        json_response(load("etherscan_block_number")),
    )
    async with httpx.AsyncClient(transport=transport) as client:
        observation = await evm_adapter(client).observe(EVM_TXID, EVM_WALLET)

    assert observation.result.verdict is Verdict.MATCHED
    assert observation.result.raw_amount == 100_000_000
    assert observation.confirmations == EXPECTED_DEPTH
    assert transport.calls == 2


async def test_the_evm_adapter_asks_for_the_head_with_the_right_chain():
    """Two calls, and the second one has to be on the same chain as the first."""
    transport = RecordingTransport(
        json_response(load("etherscan_erc20_single")),
        json_response(load("etherscan_block_number")),
    )
    async with httpx.AsyncClient(transport=transport) as client:
        await evm_adapter(client).observe(EVM_TXID, EVM_WALLET)

    first, second = transport.requests
    assert first.url.params["action"] == "eth_getTransactionReceipt"
    assert second.url.params["action"] == "eth_blockNumber"
    assert first.url.params["chainid"] == second.url.params["chainid"] == "1"


async def test_a_transaction_that_is_no_longer_there_reports_no_depth():
    """A reorg displaced it. Zero would say "seen, not deep yet" -- a lie."""
    transport = RecordingTransport(json_response(load("etherscan_result_null")))
    async with httpx.AsyncClient(transport=transport) as client:
        observation = await evm_adapter(client).observe(EVM_TXID, EVM_WALLET)

    assert observation.result.verdict is Verdict.NOT_FOUND
    assert observation.confirmations is None
    assert transport.calls == 1  # no point asking for the head


async def test_an_unreadable_head_makes_the_whole_observation_an_api_error():
    transport = RecordingTransport(
        json_response(load("etherscan_erc20_single")),
        json_response({"jsonrpc": "2.0", "id": 1, "result": "not-a-number"}),
    )
    async with httpx.AsyncClient(transport=transport) as client:
        observation = await evm_adapter(client).observe(EVM_TXID, EVM_WALLET)

    assert observation.result.verdict is Verdict.API_ERROR
    assert observation.confirmations is None


async def test_a_head_behind_the_block_is_clamped_to_zero():
    """An inconsistent node, not a broken one.

    A negative depth has no honest reading other than "not confirmed yet",
    which is what zero says. Refusing instead would turn a lagging replica into
    an outage.
    """
    transport = RecordingTransport(
        json_response(load("etherscan_erc20_single")),
        json_response({"jsonrpc": "2.0", "id": 1, "result": "0x1"}),
    )
    async with httpx.AsyncClient(transport=transport) as client:
        observation = await evm_adapter(client).observe(EVM_TXID, EVM_WALLET)

    assert observation.confirmations == 0


async def test_the_tron_adapter_reads_the_depth_from_the_same_response():
    """One call. The asymmetry with EVM is the chain's, not a design choice."""
    transport = RecordingTransport(json_response(load("tronscan_usdt_single")))
    async with httpx.AsyncClient(transport=transport) as client:
        observation = await tron_adapter(client).observe(TRON_TXID, TRON_WALLET)

    assert observation.result.verdict is Verdict.MATCHED
    assert observation.confirmations == 42
    assert transport.calls == 1


async def test_the_tron_request_still_carries_the_api_key_when_observing():
    """P-19 applies to every request, not only to the submission path."""
    transport = RecordingTransport(json_response(load("tronscan_usdt_single")))
    async with httpx.AsyncClient(transport=transport) as client:
        await tron_adapter(client).observe(TRON_TXID, TRON_WALLET)

    assert transport.requests[0].headers["TRON-PRO-API-KEY"] == "test-key-not-real"


@pytest.mark.parametrize("depth", [None, -1, "12", 12.5, True])
async def test_a_matched_tron_record_without_a_readable_depth_is_an_api_error(depth: object):
    """Matched but undatable. Reporting zero would make the worker wait forever."""
    payload = load("tronscan_usdt_single")
    if depth is None:
        payload.pop("confirmations")
    else:
        payload["confirmations"] = depth

    transport = RecordingTransport(json_response(payload))
    async with httpx.AsyncClient(transport=transport) as client:
        observation = await tron_adapter(client).observe(TRON_TXID, TRON_WALLET)

    assert observation.result.verdict is Verdict.API_ERROR
    assert observation.confirmations is None


@pytest.mark.parametrize("blank", ["", "   "])
async def test_observing_with_a_blank_wallet_is_refused_by_both_adapters(blank: str):
    """The same guard as ``lookup``: an empty address matches nothing."""
    transport = RecordingTransport()
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ValueError, match="wallet_address"):
            await evm_adapter(client).observe(EVM_TXID, blank)
        with pytest.raises(ValueError, match="wallet_address"):
            await tron_adapter(client).observe(TRON_TXID, blank)

    assert transport.calls == 0


# ==========================================================================
# The observation contract
# ==========================================================================


def test_a_match_must_carry_a_depth():
    with pytest.raises(ValueError, match="confirmations"):
        ExplorerObservation(result=ExplorerResult(verdict=Verdict.MATCHED, raw_amount=1))


def test_a_depth_cannot_be_negative():
    with pytest.raises(ValueError, match="non-negative"):
        ExplorerObservation(
            result=ExplorerResult(verdict=Verdict.MATCHED, raw_amount=1), confirmations=-1
        )


@pytest.mark.parametrize(
    "verdict", [Verdict.NOT_FOUND, Verdict.WRONG_ADDRESS, Verdict.API_ERROR]
)
def test_only_a_match_may_carry_a_depth(verdict: Verdict):
    with pytest.raises(ValueError, match="confirmations"):
        ExplorerObservation(result=ExplorerResult(verdict=verdict), confirmations=1)


def test_the_submission_result_is_unchanged_by_the_observation_type():
    """Composition, not an extra field.

    ``ExplorerResult`` gained nothing in H4. Had ``confirmations`` been added to
    it, every submission would carry a field that is always ``None`` there --
    and computing it honestly would have cost the synchronous path a second
    call on EVM.
    """
    assert "confirmations" not in {f for f in ExplorerResult.__dataclass_fields__}


# ==========================================================================
# Three axes: confirmations as an input
# ==========================================================================


@pytest.mark.parametrize("depth", [0, 1, 11, 12, 13, 10_000])
async def test_any_non_negative_depth_is_carried_through_verbatim(depth: int):
    """Including zero, which is a real answer and not an absence.

    The threshold comparison belongs to the transition function; the adapter
    reports and does not judge.
    """
    payload = load("tronscan_usdt_single")
    payload["confirmations"] = depth

    transport = RecordingTransport(json_response(payload))
    async with httpx.AsyncClient(transport=transport) as client:
        observation = await tron_adapter(client).observe(TRON_TXID, TRON_WALLET)

    assert observation.confirmations == depth


async def test_observing_the_same_transaction_twice_gives_the_same_answer():
    """No memory at this layer: depth is read, never accumulated."""
    results = []
    for _ in range(2):
        transport = RecordingTransport(json_response(load("tronscan_usdt_single")))
        async with httpx.AsyncClient(transport=transport) as client:
            results.append(await tron_adapter(client).observe(TRON_TXID, TRON_WALLET))

    assert results[0] == results[1]


def test_the_worker_knobs_have_the_defaults_the_schedule_assumes():
    """The schedule tests read these; pinning them keeps the arithmetic honest."""
    settings: Settings = make_settings()

    assert settings.WORKER_POLL_MIN_SECONDS == 30.0
    assert settings.WORKER_POLL_MAX_SECONDS == 3600.0
    assert settings.WORKER_LEASE_SECONDS == 300.0
    assert settings.WORKER_TICK_SECONDS == 5.0
