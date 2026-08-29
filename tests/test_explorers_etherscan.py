"""T-09: the Etherscan V2 adapter on chainid 1 and chainid 56.

The negative case at the top of this file is the whole reason the adapter
exists in this shape. Legacy `txid_checker.py` read the Transfer event without
checking which contract emitted it, so any ERC20 token arriving at our address
would have counted as a USDT payment. That source is gone and unavailable, so
this test is not a regression test against it -- it is the only thing standing
between us and writing the same bug from scratch.
"""

from __future__ import annotations

import httpx
import pytest

from app.domain.statuses import Verdict
from app.explorers.etherscan import EtherscanAdapter
from tests.explorers_support import (
    EVM_OTHER,
    EVM_SENDER,
    EVM_TXID,
    EVM_WALLET,
    USDT_BSC20,
    USDT_ERC20,
    RecordingTransport,
    client_for,
    json_response,
    load,
    raw_response,
)

#: Every module in this family claims it reaches no explorer. The claim is
#: what arms the transport trap in tests/conftest.py -- see the docstring
#: there for why the trap is opt-in rather than always on.
pytestmark = pytest.mark.no_network

ERC20_CHAIN = 1
BSC20_CHAIN = 56


async def ask(
    response: httpx.Response,
    *,
    contract: str = USDT_ERC20,
    chain_id: int = ERC20_CHAIN,
    wallet: str = EVM_WALLET,
    api_key: str = "test-key-not-real",
):
    """Run one lookup against one prepared response; return result + transport."""
    transport = RecordingTransport(response)
    async with client_for(transport) as client:
        adapter = EtherscanAdapter(
            client=client,
            api_url="https://api.etherscan.io/v2/api",
            api_key=api_key,
            contract_address=contract,
            chain_id=chain_id,
        )
        result = await adapter.lookup(EVM_TXID, wallet)
    return result, transport


# --------------------------------------------------------------------------
# The issuing-contract check
# --------------------------------------------------------------------------


async def test_a_foreign_tokens_transfer_to_our_address_is_not_a_payment():
    """The legacy bug, refused. TOR sections 6a and 4.

    The network is right, the recipient is right, the amount is large -- and
    the token is not USDT. TOR section 4 is explicit that this is ``not_found``
    with no code of its own: there was simply no valid USDT payment in the
    transaction.
    """
    result, _ = await ask(json_response(load("etherscan_erc20_foreign_only")))

    assert result.verdict is Verdict.NOT_FOUND
    assert result.raw_amount is None


async def test_a_foreign_token_is_refused_on_bsc_too():
    """Stated separately for BSC rather than parametrised with ERC20.

    The two chains are one class in the code and could stop being one in a
    future edit. A parametrised pair would then be changed once and silently
    stop covering the second chain; two tests have to be deleted on purpose.
    """
    result, _ = await ask(
        json_response(load("etherscan_erc20_foreign_only")),
        contract=USDT_BSC20,
        chain_id=BSC20_CHAIN,
    )

    assert result.verdict is Verdict.NOT_FOUND


async def test_our_contracts_approval_event_is_not_a_transfer():
    """Right contract, wrong event. The topic0 check, not the address check."""
    result, _ = await ask(json_response(load("etherscan_erc20_approval_only")))

    assert result.verdict is Verdict.NOT_FOUND


# --------------------------------------------------------------------------
# Matching and summing
# --------------------------------------------------------------------------


async def test_one_transfer_of_ours_to_our_address_matches():
    result, _ = await ask(json_response(load("etherscan_erc20_single")))

    assert result.verdict is Verdict.MATCHED
    assert result.raw_amount == 100_000_000
    assert result.from_address == EVM_SENDER.lower()


async def test_a_split_payment_is_summed_not_first_wins():
    """Three Transfers of ours in one transaction: 40 + 35 + 25.

    A sending wallet may split a payment. Taking the first event would credit
    40 USDT for a 100 USDT invoice and mark the invoice underpaid.
    """
    result, _ = await ask(json_response(load("etherscan_erc20_split")))

    assert result.verdict is Verdict.MATCHED
    assert result.raw_amount == 100_000_000


async def test_only_our_transfers_are_summed_when_foreign_ones_share_the_transaction():
    """The pair to the split test, and the one that catches a lazy sum.

    The foreign transfers here are far larger than ours precisely so that a
    sum over all logs cannot accidentally produce the right answer.
    """
    result, _ = await ask(json_response(load("etherscan_erc20_mixed")))

    assert result.verdict is Verdict.MATCHED
    assert result.raw_amount == 100_000_000


async def test_bsc_amounts_are_returned_undivided_at_18_decimals():
    """The adapter performs no arithmetic; 100 BSC-USD is 1e20 raw.

    If any division had leaked in here, a formula tuned to 6 decimals would
    return 1e14 cents for a 100 dollar payment and nobody downstream would be
    able to tell.
    """
    result, _ = await ask(
        json_response(load("etherscan_bsc20_single")),
        contract=USDT_BSC20,
        chain_id=BSC20_CHAIN,
    )

    assert result.verdict is Verdict.MATCHED
    assert result.raw_amount == 100 * 10**18


async def test_our_token_sent_to_somebody_else_is_wrong_address():
    result, _ = await ask(json_response(load("etherscan_erc20_wrong_recipient")))

    assert result.verdict is Verdict.WRONG_ADDRESS
    assert result.raw_amount is None
    assert result.from_address == EVM_SENDER.lower()


async def test_a_receipt_with_no_events_is_not_found():
    result, _ = await ask(json_response(load("etherscan_erc20_no_logs")))

    assert result.verdict is Verdict.NOT_FOUND


# --------------------------------------------------------------------------
# Address case -- the trap that would break every real payment
# --------------------------------------------------------------------------


async def test_a_checksummed_config_address_matches_a_lowercase_topic():
    """EIP-55 config against lower-case log data.

    ``USDT_CONTRACT_ERC20`` is stored mixed-case; topics are lower-case. A
    verbatim comparison returns ``not_found`` for every genuine payment ever
    made -- silently, and identically to a user typing a hash that does not
    exist.
    """
    assert USDT_ERC20.lower() != USDT_ERC20

    result, _ = await ask(json_response(load("etherscan_erc20_single")))

    assert result.verdict is Verdict.MATCHED


async def test_a_lowercase_configured_contract_matches_just_as_well():
    result, _ = await ask(
        json_response(load("etherscan_erc20_single")), contract=USDT_ERC20.lower()
    )

    assert result.verdict is Verdict.MATCHED


async def test_a_checksummed_wallet_matches_a_lowercase_topic():
    assert EVM_WALLET.lower() != EVM_WALLET

    result, _ = await ask(json_response(load("etherscan_erc20_single")), wallet=EVM_WALLET.lower())

    assert result.verdict is Verdict.MATCHED


# --------------------------------------------------------------------------
# The two envelopes, and the not_found / api_error boundary
# --------------------------------------------------------------------------


async def test_a_null_result_is_the_only_shape_that_means_not_indexed():
    """The single answer the retry loop above the adapter exists for."""
    result, _ = await ask(json_response(load("etherscan_result_null")))

    assert result.verdict is Verdict.NOT_FOUND


@pytest.mark.parametrize(
    "fixture", ["etherscan_notok_rate_limit", "etherscan_notok_invalid_key"]
)
async def test_the_notok_envelope_is_an_api_error_despite_its_200(fixture: str):
    """Etherscan's own envelope, on a URL that otherwise speaks JSON-RPC.

    Reading this as ``not_found`` would spend a user attempt on our throttling.
    It arrives with HTTP 200, so nothing but the body distinguishes it.

    TWO texts, one shape, and only one of the texts was ever observed. The
    capture (P-28) was taken with an invalid key, so that envelope and its
    wording are confirmed; the rate-limit wording next to it is still
    reconstruction, because nobody provoked a rate limit. The parametrisation
    says out loud what the adapter relies on -- the presence of a top-level
    ``status``, never the text -- which is exactly why one unobserved wording
    costs nothing.
    """
    payload = load(fixture)
    assert payload["status"] == "0"
    assert payload["message"] == "NOTOK"

    result, _ = await ask(json_response(payload))

    assert result.verdict is Verdict.API_ERROR
    assert result.from_address is None


async def test_a_jsonrpc_error_object_is_an_api_error():
    result, _ = await ask(json_response(load("etherscan_jsonrpc_error")))

    assert result.verdict is Verdict.API_ERROR


@pytest.mark.parametrize("status_code", [400, 401, 403, 429, 500, 502, 503])
async def test_every_non_2xx_status_is_an_api_error(status_code: int):
    result, _ = await ask(json_response({"whatever": True}, status_code=status_code))

    assert result.verdict is Verdict.API_ERROR


async def test_a_200_with_an_empty_body_is_an_api_error_not_not_found():
    """Emptiness of the transport is not emptiness of the index.

    An explorer with nothing to say about a hash says so in JSON. A zero-length
    body is a proxy, a truncation or a maintenance page, and charging a user an
    attempt for one charges them for our infrastructure.
    """
    result, _ = await ask(raw_response(b""))

    assert result.verdict is Verdict.API_ERROR


@pytest.mark.parametrize("body", [b"<html>502 Bad Gateway</html>", b"not json at all", b"{"])
async def test_a_body_that_is_not_json_is_an_api_error(body: bytes):
    result, _ = await ask(raw_response(body))

    assert result.verdict is Verdict.API_ERROR


async def test_a_transport_failure_is_an_api_error_not_an_exception():
    """A dead explorer must not become a 500 for the user."""

    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(explode)
    async with client_for(transport) as client:
        adapter = EtherscanAdapter(
            client=client,
            api_url="https://api.etherscan.io/v2/api",
            api_key="test-key-not-real",
            contract_address=USDT_ERC20,
            chain_id=ERC20_CHAIN,
        )
        result = await adapter.lookup(EVM_TXID, EVM_WALLET)

    assert result.verdict is Verdict.API_ERROR


# --------------------------------------------------------------------------
# What actually went out on the wire
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("chain_id", "contract"), [(ERC20_CHAIN, USDT_ERC20), (BSC20_CHAIN, USDT_BSC20)]
)
async def test_the_request_carries_the_right_chainid_and_action(chain_id: int, contract: str):
    """The one difference between the two chains, asserted on the wire.

    Both adapters are the same class, so the only evidence that BSC20 is not
    silently querying Ethereum is the query string itself.
    """
    _, transport = await ask(
        json_response(load("etherscan_result_null")), contract=contract, chain_id=chain_id
    )

    assert transport.calls == 1
    params = transport.requests[0].url.params
    assert params["chainid"] == str(chain_id)
    assert params["module"] == "proxy"
    assert params["action"] == "eth_getTransactionReceipt"
    assert params["txhash"] == EVM_TXID
    assert params["apikey"] == "test-key-not-real"


async def test_the_txid_goes_out_exactly_as_submitted():
    """No normalisation on the way to the explorer.

    The domain already refuses to normalise a TXID on the way in; doing it here
    would make the string we ask about differ from the string we store.
    """
    upper = "0x" + "AB" * 32
    transport = RecordingTransport(json_response(load("etherscan_result_null")))
    async with client_for(transport) as client:
        adapter = EtherscanAdapter(
            client=client,
            api_url="https://api.etherscan.io/v2/api",
            api_key="test-key-not-real",
            contract_address=USDT_ERC20,
            chain_id=ERC20_CHAIN,
        )
        await adapter.lookup(upper, EVM_WALLET)

    assert transport.requests[0].url.params["txhash"] == upper


# --------------------------------------------------------------------------
# Refusing to start misconfigured
# --------------------------------------------------------------------------


@pytest.mark.parametrize("api_key", ["", "   "])
async def test_a_blank_api_key_stops_the_adapter_from_being_built(api_key: str):
    """The sharpest of the three, because Etherscan does not complain.

    A blank key comes back with ``status: "1"`` and a quietly reduced rate
    limit, so the misconfiguration would surface weeks later as intermittent
    ``api_error`` under load rather than as a service that refuses to start.
    """
    transport = RecordingTransport()
    async with client_for(transport) as client:
        with pytest.raises(ValueError, match="api_key"):
            EtherscanAdapter(
                client=client,
                api_url="https://api.etherscan.io/v2/api",
                api_key=api_key,
                contract_address=USDT_ERC20,
                chain_id=ERC20_CHAIN,
            )


@pytest.mark.parametrize(
    "contract",
    [
        "",
        "   ",
        "0x",
        "0xdAC17F958D2ee523a2206206994597C13D831ec",  # 39 hex digits
        "dAC17F958D2ee523a2206206994597C13D831ec7",  # no prefix
        "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",  # a TRON address in an EVM slot
    ],
)
async def test_a_contract_address_that_is_not_one_stops_the_adapter(contract: str):
    """The deadliest emptiness in the service.

    ``Settings`` accepts ``""`` here -- verified against the live class. An
    empty contract matches no emitter, so every genuine payment on the network
    would come back ``not_found`` and look exactly like a user inventing a
    hash. Failing at construction turns a silent, permanent misclassification
    into a service that will not start.
    """
    transport = RecordingTransport()
    async with client_for(transport) as client:
        with pytest.raises(ValueError, match="contract address"):
            EtherscanAdapter(
                client=client,
                api_url="https://api.etherscan.io/v2/api",
                api_key="test-key-not-real",
                contract_address=contract,
                chain_id=ERC20_CHAIN,
            )


@pytest.mark.parametrize("wallet", ["", "   "])
async def test_a_blank_wallet_address_is_refused_per_call(wallet: str):
    """Checked per call, not at construction, because it is per invoice.

    TOR section 4 snapshots the address onto the invoice, so it arrives with
    the lookup. An empty one is corrupt data rather than a domain state, and
    comparing recipients against ``""`` would turn every payment into
    ``not_found``.
    """
    transport = RecordingTransport()
    async with client_for(transport) as client:
        adapter = EtherscanAdapter(
            client=client,
            api_url="https://api.etherscan.io/v2/api",
            api_key="test-key-not-real",
            contract_address=USDT_ERC20,
            chain_id=ERC20_CHAIN,
        )
        with pytest.raises(ValueError, match="wallet_address"):
            await adapter.lookup(EVM_TXID, wallet)

    assert transport.calls == 0


async def test_a_wallet_from_the_wrong_chain_matches_nothing_rather_than_everything():
    """A TRON address on an EVM invoice: no crash, no false match."""
    result, _ = await ask(
        json_response(load("etherscan_erc20_single")), wallet="TWbdVwjHTNn2PXDPbtSNvvESDd8PpApFmX"
    )

    assert result.verdict is Verdict.WRONG_ADDRESS
    assert result.from_address == EVM_SENDER.lower()


async def test_a_different_evm_wallet_does_not_match():
    result, _ = await ask(json_response(load("etherscan_erc20_single")), wallet=EVM_OTHER)

    assert result.verdict is Verdict.WRONG_ADDRESS
