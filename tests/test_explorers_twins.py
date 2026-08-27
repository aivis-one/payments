"""Negative twins for the explorer layer: repeat / emptiness / shortfall.

Four inputs cross this boundary -- the explorer's response, the TXID, the
contract address that arrives from config, and the wallet address that arrives
from the invoice snapshot. Each gets all three axes.

Emptiness carries most of the weight here, and not because there are more ways
to be empty: because the ways to be empty **do not all mean the same thing**.
``{}`` from TronScan is a content answer, ``[]`` from the same endpoint is a
broken one, an empty ``logs`` array is a real transaction that emitted nothing
and an empty body is no answer at all. Collapsing them would move the boundary
that decides whether a user's attempt is spent.

Deliberately malformed payloads are built inline rather than stored in
``fixtures/``: no explorer sends them, and a file claiming otherwise would
misrepresent what was frozen. See ``fixtures/PROVENANCE.md``.
"""

from __future__ import annotations

import pytest

from app.domain.statuses import Verdict
from app.explorers.etherscan import EtherscanAdapter
from app.explorers.tronscan import TronScanAdapter
from app.explorers.verify import verify_txid
from tests.explorers_support import (
    EVM_SENDER,
    EVM_TXID,
    EVM_WALLET,
    TRON_TXID,
    TRON_WALLET,
    USDT_BSC20,
    USDT_ERC20,
    USDT_TRC20,
    RecordingTransport,
    client_for,
    json_response,
    load,
    make_settings,
    raw_response,
)

#: Every module in this family claims it reaches no explorer. The claim is
#: what arms the transport trap in tests/conftest.py -- see the docstring
#: there for why the trap is opt-in rather than always on.
pytestmark = pytest.mark.no_network

ETHERSCAN_URL = "https://api.etherscan.io/v2/api"
TRONSCAN_URL = "https://apilist.tronscanapi.com/api"
TRONSCAN_KEY = "test-key-not-real"


async def evm(response, *, contract=USDT_ERC20, wallet=EVM_WALLET, chain_id=1):
    transport = RecordingTransport(response)
    async with client_for(transport) as client:
        adapter = EtherscanAdapter(
            client=client,
            api_url=ETHERSCAN_URL,
            api_key="test-key-not-real",
            contract_address=contract,
            chain_id=chain_id,
        )
        return await adapter.lookup(EVM_TXID, wallet)


async def tron(response, *, contract=USDT_TRC20, wallet=TRON_WALLET):
    transport = RecordingTransport(response)
    async with client_for(transport) as client:
        adapter = TronScanAdapter(
            client=client, api_url=TRONSCAN_URL, api_key=TRONSCAN_KEY, contract_address=contract
        )
        return await adapter.lookup(TRON_TXID, wallet)


# ==========================================================================
# Input 1: the explorer response
# ==========================================================================

# -- REPEAT ----------------------------------------------------------------


async def test_the_same_response_twice_gives_the_same_result():
    """The adapter is a function of its input; it remembers no earlier call."""
    first = await evm(json_response(load("etherscan_erc20_single")))
    second = await evm(json_response(load("etherscan_erc20_single")))

    assert first == second


async def test_two_identical_transfer_logs_are_summed_not_deduplicated():
    """Repeat inside one response is quantity, not duplication.

    Two Transfer events of the same amount between the same parties in one
    transaction are two real movements of money. A layer that deduplicated
    them would silently halve a payment. The chain cannot emit the same
    ``logIndex`` twice, so there is no case where dropping one is right.
    """
    payload = load("etherscan_erc20_single")
    logs = payload["result"]["logs"]
    payload["result"]["logs"] = [logs[0], dict(logs[0])]

    result = await evm(json_response(payload))

    assert result.verdict is Verdict.MATCHED
    assert result.raw_amount == 200_000_000


async def test_two_identical_trc20_entries_are_summed_too():
    payload = load("tronscan_usdt_single")
    entries = payload["trc20TransferInfo"]
    payload["trc20TransferInfo"] = [entries[0], dict(entries[0])]

    result = await tron(json_response(payload))

    assert result.verdict is Verdict.MATCHED
    assert result.raw_amount == 200_000_000


# -- EMPTINESS -------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"", Verdict.API_ERROR),  # no answer at all
        (b"{}", Verdict.API_ERROR),  # JSON-RPC without result or error
        (b"[]", Verdict.API_ERROR),  # not an object
        (b"null", Verdict.API_ERROR),  # not an object
        (b'{"jsonrpc":"2.0","id":1,"result":null}', Verdict.NOT_FOUND),  # not indexed
        (b'{"jsonrpc":"2.0","id":1,"result":{"logs":[]}}', Verdict.NOT_FOUND),  # emitted nothing
    ],
)
async def test_the_six_empty_shapes_of_an_etherscan_answer(body: bytes, expected: Verdict):
    """Six ways to be empty, two verdicts. The split is the point.

    Only the last two say something about the transaction. The first four say
    something about the response, and charging a user an attempt for a
    malformed response charges them for our plumbing.
    """
    assert await evm(raw_response(body)) == await evm(raw_response(body))
    result = await evm(raw_response(body))

    assert result.verdict is expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b"", Verdict.API_ERROR),
        (b"{}", Verdict.NOT_FOUND),  # TronScan's "no such transaction"
        (b"[]", Verdict.API_ERROR),
        (b"null", Verdict.API_ERROR),
        (b'{"contractType":31,"hash":"a2","trc20TransferInfo":[]}', Verdict.NOT_FOUND),
    ],
)
async def test_the_empty_shapes_of_a_tronscan_answer(body: bytes, expected: Verdict):
    """``{}`` parts company with ``[]`` and ``null`` here, deliberately.

    An object endpoint answering with an empty object is answering. Answering
    with an array is not.
    """
    result = await tron(raw_response(body))

    assert result.verdict is expected


# -- SHORTFALL -------------------------------------------------------------


async def test_a_receipt_without_a_logs_key_is_an_api_error_not_a_quiet_match():
    """Missing key and empty array are different claims.

    An empty ``logs`` array means "this transaction emitted nothing", which is
    a content answer worth an attempt. An absent key means the response is not
    the shape we parse, and reading it as the former would spend an attempt on
    a payload we failed to understand. This is a deliberate refinement of the
    ladder as first written -- see the delivery report.
    """
    payload = load("etherscan_erc20_single")
    del payload["result"]["logs"]

    result = await evm(json_response(payload))

    assert result.verdict is Verdict.API_ERROR


async def test_a_transfer_of_ours_with_no_data_field_refuses_rather_than_skips():
    """Skipping would under-count money; guessing would invent it.

    Once the emitter and the event signature say this is a Transfer of our own
    contract, an unreadable amount is not something to step over: in a split
    payment, stepping over one event credits less than arrived.
    """
    payload = load("etherscan_erc20_split")
    del payload["result"]["logs"][1]["data"]

    result = await evm(json_response(payload))

    assert result.verdict is Verdict.API_ERROR


@pytest.mark.parametrize("data", ["", "0x", "not-hex", "0xzz"])
async def test_an_unreadable_amount_refuses_the_whole_answer(data: str):
    payload = load("etherscan_erc20_single")
    payload["result"]["logs"][0]["data"] = data

    result = await evm(json_response(payload))

    assert result.verdict is Verdict.API_ERROR


async def test_a_transfer_topic_with_too_few_topics_refuses():
    """Our contract, the Transfer signature, and no recipient to read."""
    payload = load("etherscan_erc20_single")
    payload["result"]["logs"][0]["topics"] = payload["result"]["logs"][0]["topics"][:2]

    result = await evm(json_response(payload))

    assert result.verdict is Verdict.API_ERROR


async def test_a_log_with_no_topics_at_all_is_skipped_not_refused():
    """Anonymous events exist and are not Transfers.

    Unlike the case above, nothing here claims to be a Transfer of ours, so
    there is no money at stake in passing it by.
    """
    payload = load("etherscan_erc20_split")
    payload["result"]["logs"][1]["topics"] = []

    result = await evm(json_response(payload))

    assert result.verdict is Verdict.MATCHED
    assert result.raw_amount == 65_000_000  # 40 + 25, the readable ones


@pytest.mark.parametrize("field", ["contract_address", "to_address", "amount_str"])
async def test_a_trc20_entry_missing_a_field_does_not_raise(field: str):
    """A missing contract makes the entry somebody else's; the rest refuse.

    Without ``contract_address`` there is no claim that this is our token, so
    it is skipped like any foreign transfer. Without a recipient or an amount,
    an entry that *is* ours cannot be read, and refusing beats guessing.
    """
    payload = load("tronscan_usdt_single")
    del payload["trc20TransferInfo"][0][field]

    result = await tron(json_response(payload))

    expected = Verdict.NOT_FOUND if field == "contract_address" else Verdict.API_ERROR
    assert result.verdict is expected


@pytest.mark.parametrize("amount", ["", "   ", "abc", "-5", "1.5"])
async def test_a_trc20_amount_that_is_not_a_whole_number_refuses(amount: str):
    """Including the negative: an on-chain amount below zero does not exist."""
    payload = load("tronscan_usdt_single")
    payload["trc20TransferInfo"][0]["amount_str"] = amount

    result = await tron(json_response(payload))

    assert result.verdict is Verdict.API_ERROR


async def test_a_trc20_entry_falls_back_to_the_numeric_amount():
    """``amount_str`` is preferred, but its absence is not fatal on its own."""
    payload = load("tronscan_usdt_single")
    del payload["trc20TransferInfo"][0]["amount_str"]
    payload["trc20TransferInfo"][0]["amount"] = 100_000_000

    result = await tron(json_response(payload))

    assert result.verdict is Verdict.MATCHED
    assert result.raw_amount == 100_000_000


# ==========================================================================
# Input 2: the TXID
# ==========================================================================


async def test_submitting_the_same_txid_twice_gives_the_same_verdict():
    """No memory at this layer. ``already_used`` belongs to the unique index."""
    results = []
    for _ in range(2):
        transport = RecordingTransport(json_response(load("etherscan_erc20_single")))
        async with client_for(transport) as client:
            results.append(
                await verify_txid(
                    network="USDT-ERC20",
                    txid=EVM_TXID,
                    wallet_address=EVM_WALLET,
                    settings=make_settings(),
                    client=client,
                )
            )

    assert results[0] == results[1]
    assert results[0].verdict is Verdict.MATCHED


@pytest.mark.parametrize("txid", ["", "   ", "0x", "0x" + "ab" * 31])
async def test_an_empty_or_short_txid_costs_nothing_and_calls_nobody(txid: str):
    transport = RecordingTransport()
    async with client_for(transport) as client:
        result = await verify_txid(
            network="USDT-ERC20",
            txid=txid,
            wallet_address=EVM_WALLET,
            settings=make_settings(),
            client=client,
        )

    assert result.verdict is Verdict.INVALID_FORMAT
    assert result.raw_amount is None
    assert result.from_address is None
    assert transport.calls == 0


# ==========================================================================
# Input 3: the contract address from config
# ==========================================================================


@pytest.mark.parametrize("blank", ["", "   "])
async def test_a_blank_contract_stops_both_adapters_from_being_built(blank: str):
    """The pair the acceptance rule demands.

    "No transfer of our contract was found" only means anything if there *is*
    a contract to look for. Without this check an empty setting would match
    nothing and every genuine payment would be reported as ``not_found`` --
    the same shape as the empty-password acceptance that once let a database
    come up unauthenticated.
    """
    transport = RecordingTransport()
    async with client_for(transport) as client:
        with pytest.raises(ValueError, match="contract address"):
            EtherscanAdapter(
                client=client,
                api_url=ETHERSCAN_URL,
                api_key="test-key-not-real",
                contract_address=blank,
                chain_id=1,
            )
        with pytest.raises(ValueError, match="contract address"):
            TronScanAdapter(
                client=client,
                api_url=TRONSCAN_URL,
                api_key=TRONSCAN_KEY,
                contract_address=blank,
            )


async def test_a_truncated_contract_does_not_match_by_prefix():
    """Refused at construction rather than half-matching a real address."""
    transport = RecordingTransport()
    async with client_for(transport) as client:
        with pytest.raises(ValueError, match="contract address"):
            EtherscanAdapter(
                client=client,
                api_url=ETHERSCAN_URL,
                api_key="test-key-not-real",
                contract_address=USDT_ERC20[:-1],
                chain_id=1,
            )


async def test_the_same_contract_configured_for_two_networks_is_not_detected():
    """Named because it is invisible, not because it is handled.

    Pasting the Ethereum USDT address into ``USDT_CONTRACT_BSC20`` produces an
    adapter that queries chain 56 for a contract that does not live there. Each
    lookup on its own is well-formed, so nothing in this layer can tell. The
    18-decimals-versus-6 difference would then make every BSC amount wrong by
    a factor of a trillion. Detecting it needs a chain-versus-contract check
    that is not in this task; the delivery report carries it as an observation.
    """
    settings = make_settings(USDT_CONTRACT_BSC20=USDT_ERC20)

    transport = RecordingTransport(json_response(load("etherscan_erc20_single")))
    async with client_for(transport) as client:
        result = await verify_txid(
            network="USDT-BSC20",
            txid=EVM_TXID,
            wallet_address=EVM_WALLET,
            settings=settings,
            client=client,
        )

    # Matched, on the wrong chain, with no complaint from anywhere.
    assert result.verdict is Verdict.MATCHED
    assert settings.USDT_CONTRACT_BSC20 != USDT_BSC20


# ==========================================================================
# Input 4: the wallet address from the invoice snapshot
# ==========================================================================


async def test_two_invoices_sharing_a_wallet_both_match_the_same_transaction():
    """Address reuse is the model; the TXID index is what stops double credit.

    TOR section 4 snapshots one configured address onto every invoice of a
    network, so two open invoices necessarily share it. Nothing in this layer
    should try to arbitrate between them -- the partial unique index on
    ``(network, txid)`` does that, and it does it in the database.
    """
    first = await evm(json_response(load("etherscan_erc20_single")))
    second = await evm(json_response(load("etherscan_erc20_single")))

    assert first.verdict is second.verdict is Verdict.MATCHED


@pytest.mark.parametrize("blank", ["", "   "])
async def test_a_blank_wallet_never_matches_anything(blank: str):
    """The other half of the empty-comparison trap.

    An empty recipient compared against real ones matches nothing, so the
    invoice would report ``not_found`` for a payment that arrived. Raising
    makes the corrupt row visible instead of quietly denying the money.
    """
    with pytest.raises(ValueError, match="wallet_address"):
        await evm(json_response(load("etherscan_erc20_single")), wallet=blank)
    with pytest.raises(ValueError, match="wallet_address"):
        await tron(json_response(load("tronscan_usdt_single")), wallet=blank)


async def test_a_truncated_wallet_does_not_match_by_prefix():
    """Guards the comparison against ever becoming a ``startswith``.

    A wallet one character short is not "nearly right"; crediting an invoice
    on a prefix would send money to whoever registered the near-miss.
    """
    result = await evm(json_response(load("etherscan_erc20_single")), wallet=EVM_WALLET[:-1])

    assert result.verdict is Verdict.WRONG_ADDRESS
    assert result.from_address == EVM_SENDER.lower()


async def test_a_wallet_with_extra_characters_does_not_match_either():
    result = await evm(json_response(load("etherscan_erc20_single")), wallet=EVM_WALLET + "00")

    assert result.verdict is Verdict.WRONG_ADDRESS
