"""Etherscan V2 adapter: ERC20 on ``chainid`` 1, BSC20 on ``chainid`` 56 (T-09).

One class serves both chains. They differ in exactly two values -- the chain
id and the USDT contract address -- and neither difference reaches the parsing
code, so a second class would be a copy waiting to drift.

**The legacy bug this closes.** ``txid_checker.py`` read the Transfer event out
of a receipt without checking which contract emitted it, so a transfer of any
ERC20 token to our address would have been accepted as a USDT payment. Here
the emitter is checked first and everything else is dropped, which is why the
negative test is not optional decoration.

**Two envelopes on one URL.** ``module=proxy`` speaks JSON-RPC, where a hash
nobody has heard of comes back as ``result: null``. But the rate limiter sits
in front of the module dispatcher and answers the same URL in Etherscan's own
envelope, ``{"status": "0", "message": "NOTOK", ...}``, with a ``200``. An
adapter that knew only the first shape would read a rate limit as "this
transaction does not exist" and burn a user attempt on our own throttling.
The two are told apart by a top-level ``status`` key, which the JSON-RPC
envelope does not have (a receipt's own ``status`` is nested inside
``result``).

**Address case.** Topics carry addresses in lower-case hex; TOR section 10
stores the contract in EIP-55 mixed case. Comparing the two verbatim would
return ``not_found`` for every real payment, so both sides are lower-cased
here. TronScan's base58 must *not* get the same treatment -- see that adapter.
"""

from __future__ import annotations

import re
from typing import Final

import httpx

from app.domain.addresses import EVM_ADDRESS
from app.domain.statuses import Verdict
from app.explorers.matching import TRANSFER_TOPIC, Transfer, classify
from app.explorers.protocol import ExplorerResult
from app.explorers.transport import ExplorerUnavailable, fetch_json

_HEX_QUANTITY: Final[re.Pattern[str]] = re.compile(r"0x[0-9a-fA-F]+")


def _address_from_topic(topic: object) -> str:
    """Take the low 20 bytes of an indexed address topic, lower-cased."""
    if not isinstance(topic, str) or len(topic) < 42:
        raise ValueError(f"topic is not an address: {topic!r}")
    return "0x" + topic[-40:].lower()


def _amount_from_data(data: object) -> int:
    """Read the unsigned integer a Transfer carries in its data field."""
    if not isinstance(data, str) or _HEX_QUANTITY.fullmatch(data) is None:
        raise ValueError(f"data is not a hex quantity: {data!r}")
    return int(data, 16)


class EtherscanAdapter:
    """Reads one transaction receipt and decides what it means for one invoice."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        api_url: str,
        api_key: str,
        contract_address: str,
        chain_id: int,
    ) -> None:
        """Build an adapter for one chain, refusing to start misconfigured.

        Every check here guards a value that is reachable from the
        environment: ``Settings`` accepts empty strings for all three of them
        (verified against the live class, not its annotations), and pydantic
        will not reject ``""`` for a mandatory ``str``.

        An empty API key is the sharpest of the three because Etherscan does
        not refuse it -- a blank key comes back with ``status: "1"`` and a
        silently degraded rate limit, so the misconfiguration would surface
        much later as intermittent ``api_error`` under load rather than as a
        failure to boot. An empty contract address is the deadliest: every
        emitter comparison would fail and every genuine payment would be
        classified ``not_found``.

        Raises:
            ValueError: on a blank URL or key, or a contract address that is
                not a 20-byte hex address.
        """
        if not api_url.strip():
            raise ValueError("etherscan api_url must not be blank")
        if not api_key.strip():
            raise ValueError("etherscan api_key must not be blank")
        if EVM_ADDRESS.fullmatch(contract_address.strip()) is None:
            raise ValueError(f"not an EVM contract address: {contract_address!r}")

        self._client = client
        self._api_url = api_url.strip()
        self._api_key = api_key.strip()
        self._contract = contract_address.strip().lower()
        self._chain_id = chain_id

    async def lookup(self, txid: str, wallet_address: str) -> ExplorerResult:
        """Fetch the receipt for ``txid`` and classify it. See the protocol."""
        if not wallet_address.strip():
            raise ValueError("wallet_address must not be blank")

        params = {
            "chainid": str(self._chain_id),
            "module": "proxy",
            "action": "eth_getTransactionReceipt",
            "txhash": txid,
            "apikey": self._api_key,
        }
        try:
            payload = await fetch_json(self._client, self._api_url, params)
        except ExplorerUnavailable:
            return ExplorerResult(verdict=Verdict.API_ERROR)

        return self._classify(payload, wallet_address.strip().lower())

    def _classify(self, payload: object, wallet_address: str) -> ExplorerResult:
        if not isinstance(payload, dict):
            return ExplorerResult(verdict=Verdict.API_ERROR)

        # Etherscan's own envelope, i.e. rate limit, invalid key, unsupported
        # chainid. Checked before anything else: it arrives with a 200.
        if payload.get("status") == "0":
            return ExplorerResult(verdict=Verdict.API_ERROR)
        if "error" in payload:
            return ExplorerResult(verdict=Verdict.API_ERROR)
        if "result" not in payload:
            return ExplorerResult(verdict=Verdict.API_ERROR)

        result = payload["result"]
        if result is None:
            # The one shape that means "no such transaction in the index" --
            # and the only reason the retry loop above this adapter exists.
            return ExplorerResult(verdict=Verdict.NOT_FOUND)
        if not isinstance(result, dict):
            return ExplorerResult(verdict=Verdict.API_ERROR)

        logs = result.get("logs")
        if not isinstance(logs, list):
            # A receipt without a logs array is a broken shape, not a quiet
            # transaction: an empty list is how "emitted nothing" is spelled.
            # Reading the former as the latter would spend a user attempt on a
            # response we could not parse.
            return ExplorerResult(verdict=Verdict.API_ERROR)

        transfers: list[Transfer] = []
        for entry in logs:
            if not isinstance(entry, dict):
                continue

            emitter = entry.get("address")
            if not isinstance(emitter, str) or emitter.strip().lower() != self._contract:
                continue

            topics = entry.get("topics")
            if not isinstance(topics, list) or not topics:
                continue
            signature = topics[0]
            if not isinstance(signature, str) or signature.strip().lower() != TRANSFER_TOPIC:
                # Our contract, some other event. Approvals live here.
                continue

            # Past this point the log is a Transfer of our own contract, so an
            # unreadable field is not something to skip past: skipping would
            # silently under-count a split payment. Refuse the whole answer
            # instead and let the user retry without being charged.
            if len(topics) != 3:
                return ExplorerResult(verdict=Verdict.API_ERROR)
            try:
                sender = _address_from_topic(topics[1])
                recipient = _address_from_topic(topics[2])
                raw_amount = _amount_from_data(entry.get("data"))
            except ValueError:
                return ExplorerResult(verdict=Verdict.API_ERROR)

            transfers.append(
                Transfer(recipient=recipient, raw_amount=raw_amount, sender=sender)
            )

        return classify(transfers, wallet_address)
