"""T-10: the TronScan adapter.

The same negative case as T-09, stated separately for TRON rather than shared
with it. TOR section 6a says the issuing-contract check "exists naturally" on
this chain because ``trc20TransferInfo`` names the token -- but the field only
helps if it is compared against something, and an adapter that read the amount
out of it without looking at ``contract_address`` would reproduce the EVM
legacy bug on a chain where the fix was supposed to be free.
"""

from __future__ import annotations

import httpx
import pytest

from app.domain.statuses import Verdict
from app.explorers.tronscan import TronScanAdapter
from tests.explorers_support import (
    TRON_OTHER,
    TRON_SENDER,
    TRON_TXID,
    TRON_WALLET,
    USDT_ERC20,
    USDT_TRC20,
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

API_URL = "https://apilist.tronscanapi.com/api"

#: TOR section 10 made the key mandatory; the adapter refuses a blank one.
TRONSCAN_KEY = "test-key-not-real"


async def ask(
    response: httpx.Response,
    *,
    contract: str = USDT_TRC20,
    wallet: str = TRON_WALLET,
):
    transport = RecordingTransport(response)
    async with client_for(transport) as client:
        adapter = TronScanAdapter(
            client=client,
            api_url=API_URL,
            api_key=TRONSCAN_KEY,
            contract_address=contract,
        )
        result = await adapter.lookup(TRON_TXID, wallet)
    return result, transport


# --------------------------------------------------------------------------
# The issuing-contract check
# --------------------------------------------------------------------------


async def test_an_arbitrary_trc20_token_to_our_address_is_not_a_payment():
    """T-10's required negative. A real TRC20 transfer, of the wrong token."""
    result, _ = await ask(json_response(load("tronscan_foreign_token")))

    assert result.verdict is Verdict.NOT_FOUND
    assert result.raw_amount is None


async def test_an_upper_cased_contract_never_gets_as_far_as_a_comparison():
    """Case-folding a base58 address does not normalise it, it breaks it.

    Upper-casing introduces ``O`` and ``I``, which base58 does not contain, so
    the shape check refuses the value at construction. That is the right layer:
    the mistake is in config, and the alternative -- accepting it and returning
    ``not_found`` for every payment on the network -- is indistinguishable from
    users inventing hashes.

    This is why the EVM adapter's lower-casing must not be copied here. It is
    also why the comparison itself is tested below with a response, not with a
    setting: config can no longer carry a mis-cased contract this far.
    """
    transport = RecordingTransport()
    async with client_for(transport) as client:
        with pytest.raises(ValueError, match="contract address"):
            TronScanAdapter(
                client=client,
                api_url=API_URL,
                api_key=TRONSCAN_KEY,
                contract_address=USDT_TRC20.upper(),
            )


async def test_a_contract_in_the_response_is_compared_verbatim():
    """The comparison itself, probed from the response side.

    The payload is altered by hand rather than loaded whole: no explorer would
    report a mis-cased contract, so a file claiming to be a frozen response
    would be lying about what TronScan sends.
    """
    payload = load("tronscan_usdt_single")
    payload["trc20TransferInfo"][0]["contract_address"] = USDT_TRC20.upper()

    result, _ = await ask(json_response(payload))

    assert result.verdict is Verdict.NOT_FOUND


async def test_a_case_folded_wallet_does_not_match():
    """Same rule on the other operand: the recipient is compared verbatim too."""
    result, _ = await ask(json_response(load("tronscan_usdt_single")), wallet=TRON_WALLET.upper())

    assert result.verdict is Verdict.WRONG_ADDRESS


# --------------------------------------------------------------------------
# Matching and summing
# --------------------------------------------------------------------------


async def test_one_usdt_transfer_to_our_address_matches():
    result, _ = await ask(json_response(load("tronscan_usdt_single")))

    assert result.verdict is Verdict.MATCHED
    assert result.raw_amount == 100_000_000
    assert result.from_address == TRON_SENDER


async def test_a_split_payment_is_summed():
    result, _ = await ask(json_response(load("tronscan_usdt_split")))

    assert result.verdict is Verdict.MATCHED
    assert result.raw_amount == 100_000_000


async def test_only_our_token_is_summed_in_a_mixed_transaction():
    """Foreign entries deliberately larger than ours, so a sum over all fails."""
    result, _ = await ask(json_response(load("tronscan_mixed")))

    assert result.verdict is Verdict.MATCHED
    assert result.raw_amount == 100_000_000


async def test_usdt_sent_to_somebody_else_is_wrong_address():
    result, _ = await ask(json_response(load("tronscan_wrong_recipient")))

    assert result.verdict is Verdict.WRONG_ADDRESS
    assert result.raw_amount is None
    assert result.from_address == TRON_SENDER


async def test_a_different_tron_wallet_does_not_match():
    result, _ = await ask(json_response(load("tronscan_usdt_single")), wallet=TRON_OTHER)

    assert result.verdict is Verdict.WRONG_ADDRESS


# --------------------------------------------------------------------------
# Content answers that are not payments
# --------------------------------------------------------------------------


async def test_an_empty_object_is_how_tronscan_says_no_such_transaction():
    """One of the two fixtures whose real shape is unverified.

    If a live capture shows something other than ``{}`` here, this is the test
    that moves. See ``fixtures/PROVENANCE.md``.
    """
    assert load("tronscan_not_found") == {}

    result, _ = await ask(json_response(load("tronscan_not_found")))

    assert result.verdict is Verdict.NOT_FOUND


async def test_a_plain_trx_transfer_is_not_a_token_payment():
    """``contractType`` 1: TRX moved, no contract called at all."""
    result, _ = await ask(json_response(load("tronscan_trx_transfer")))

    assert result.verdict is Verdict.NOT_FOUND


async def test_a_contract_call_that_moved_no_trc20_is_not_found():
    """``contractType`` 31 covers every contract call, not just token transfers.

    So a TriggerSmartContract with no ``trc20TransferInfo`` is an ordinary
    content answer, not a broken response, and must not become ``api_error``.
    """
    payload = load("tronscan_no_transfer_info")
    assert payload["contractType"] == 31
    assert "trc20TransferInfo" not in payload

    result, _ = await ask(json_response(payload))

    assert result.verdict is Verdict.NOT_FOUND


async def test_a_reverted_transaction_moved_nothing():
    """It exists on chain and it failed. The transfer info is still there."""
    result, _ = await ask(json_response(load("tronscan_reverted")))

    assert result.verdict is Verdict.NOT_FOUND


# --------------------------------------------------------------------------
# Not answers at all
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status_code", [400, 401, 403, 429, 500, 502, 503])
async def test_every_non_2xx_status_is_an_api_error(status_code: int):
    result, _ = await ask(json_response({}, status_code=status_code))

    assert result.verdict is Verdict.API_ERROR


async def test_a_200_with_an_empty_body_is_an_api_error():
    result, _ = await ask(raw_response(b""))

    assert result.verdict is Verdict.API_ERROR


@pytest.mark.parametrize("body", [b"<html>rate limited</html>", b"null", b"[]", b'"a string"'])
async def test_a_body_that_is_not_an_object_is_an_api_error(body: bytes):
    """``null`` and ``[]`` part company with ``{}`` here, on purpose.

    An endpoint documented to return an object answering with something that
    is not an object says nothing about whether the transaction exists -- it
    says the response is broken. ``{}`` is the opposite: the right type,
    carrying the answer "nothing".
    """
    result, _ = await ask(raw_response(body))

    assert result.verdict is Verdict.API_ERROR


async def test_a_transport_failure_is_an_api_error_not_an_exception():
    def explode(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    transport = httpx.MockTransport(explode)
    async with client_for(transport) as client:
        adapter = TronScanAdapter(
            client=client,
            api_url=API_URL,
            api_key=TRONSCAN_KEY,
            contract_address=USDT_TRC20,
        )
        result = await adapter.lookup(TRON_TXID, TRON_WALLET)

    assert result.verdict is Verdict.API_ERROR


# --------------------------------------------------------------------------
# What actually went out on the wire
# --------------------------------------------------------------------------


async def test_the_request_hits_transaction_info_with_the_hash():
    _, transport = await ask(json_response(load("tronscan_not_found")))

    assert transport.calls == 1
    request = transport.requests[0]
    assert request.url.path == "/api/transaction-info"
    assert request.url.params["hash"] == TRON_TXID


@pytest.mark.parametrize("api_url", [API_URL, API_URL + "/"])
async def test_a_trailing_slash_in_the_configured_url_does_not_double_up(api_url: str):
    """The default in TOR section 10 has no trailing slash; an operator's may."""
    transport = RecordingTransport(json_response(load("tronscan_not_found")))
    async with client_for(transport) as client:
        adapter = TronScanAdapter(
            client=client,
            api_url=api_url,
            api_key=TRONSCAN_KEY,
            contract_address=USDT_TRC20,
        )
        await adapter.lookup(TRON_TXID, TRON_WALLET)

    assert transport.requests[0].url.path == "/api/transaction-info"


# --------------------------------------------------------------------------
# Refusing to start misconfigured
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "contract",
    [
        "",
        "   ",
        "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6",  # one character short
        "R7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",  # no leading T
        USDT_ERC20,  # an EVM address in a TRON slot
        "T0OIl" + "1" * 29,  # characters base58 does not contain
    ],
)
async def test_a_contract_address_that_is_not_one_stops_the_adapter(contract: str):
    transport = RecordingTransport()
    async with client_for(transport) as client:
        with pytest.raises(ValueError, match="contract address"):
            TronScanAdapter(
                client=client,
                api_url=API_URL,
                api_key=TRONSCAN_KEY,
                contract_address=contract,
            )


@pytest.mark.parametrize("api_url", ["", "   "])
async def test_a_blank_api_url_stops_the_adapter(api_url: str):
    transport = RecordingTransport()
    async with client_for(transport) as client:
        with pytest.raises(ValueError, match="api_url"):
            TronScanAdapter(
                client=client,
                api_url=api_url,
                api_key=TRONSCAN_KEY,
                contract_address=USDT_TRC20,
            )


@pytest.mark.parametrize("wallet", ["", "   "])
async def test_a_blank_wallet_address_is_refused_per_call(wallet: str):
    transport = RecordingTransport()
    async with client_for(transport) as client:
        adapter = TronScanAdapter(
            client=client,
            api_url=API_URL,
            api_key=TRONSCAN_KEY,
            contract_address=USDT_TRC20,
        )
        with pytest.raises(ValueError, match="wallet_address"):
            await adapter.lookup(TRON_TXID, wallet)

    assert transport.calls == 0


# --------------------------------------------------------------------------
# P-28: the field the live capture brought, and the trap in it
# --------------------------------------------------------------------------


async def test_the_top_level_to_address_is_the_contract_and_is_not_read():
    """The one trap the live TronScan response carries.

    A real ``transaction-info`` answer has ``toAddress`` at the TOP LEVEL, and
    it holds the address of the **contract** -- for a TRC20 transfer the
    transaction is addressed to the token, and the recipient exists only inside
    ``trc20TransferInfo[].to_address``. An adapter that reached for the obvious
    top-level field would return ``wrong_address`` for every genuine payment
    this service will ever see.

    The field was absent from the reconstruction, so nothing here could have
    gone wrong; it arrived with the capture, and it is asserted rather than
    merely stored, because a trap that is present in the data and absent from
    the tests is a trap waiting for the next reader.
    """
    payload = load("tronscan_usdt_single")
    assert payload["toAddress"] == USDT_TRC20
    assert payload["toAddress"] != payload["trc20TransferInfo"][0]["to_address"]

    result, _ = await ask(json_response(payload))

    assert result.verdict is Verdict.MATCHED


async def test_the_capture_fields_do_not_disturb_the_verdict():
    """The rest of the live shape -- twenty-odd fields the adapter ignores.

    Stated once, so that the merge of the capture into this fixture is covered
    by something more than the tests that happened to already exist.
    """
    payload = load("tronscan_usdt_single")
    for field in ("contract_map", "contractInfo", "trigger_info", "tokenTransferInfo",
                  "transfersAllList", "srConfirmList", "normalAddressInfo", "contractData"):
        assert field in payload

    result, _ = await ask(json_response(payload))

    assert result.verdict is Verdict.MATCHED
    assert result.raw_amount == int(payload["trc20TransferInfo"][0]["amount_str"])
    assert result.from_address == payload["trc20TransferInfo"][0]["from_address"]
