"""TronScan adapter for USDT-TRC20 (T-10).

TRON reports token movements as structured data rather than as raw logs:
``contractType`` 31 is ``TriggerSmartContract`` and ``trc20TransferInfo``
lists the TRC20 movements the call produced, already decoded. That is why TOR
section 6a says the issuing-contract check "exists naturally" here -- the
field is right there. It still has to be *used*: ``trc20TransferInfo`` names
whichever token the transaction moved, and accepting it without comparing
``contract_address`` against USDT-TRC20 would reproduce the EVM legacy bug on
a different chain.

**Base58 is case-sensitive.** TRON addresses carry a checksum over their exact
characters; lower-casing one destroys it and turns a valid address into a
different, invalid string. The EVM adapter must lower-case and this one must
not. The asymmetry is real, not an oversight, and it is the reason the two
adapters normalise in their own code rather than through a shared helper.

**``contractType`` 31 is not a synonym for "token transfer".** It covers every
contract call on TRON, so a call that moved no TRC20 at all is a perfectly
ordinary content answer and becomes ``not_found`` rather than an error.
"""

from __future__ import annotations

import re
from typing import Final

import httpx

from app.domain.statuses import Verdict
from app.explorers.matching import Transfer, classify
from app.explorers.protocol import ExplorerResult
from app.explorers.transport import ExplorerUnavailable, fetch_json

#: ``TriggerSmartContract``. Any contract call, token or not.
TRIGGER_SMART_CONTRACT: Final[int] = 31

#: Shape smoke-check only -- length and leading ``T``. Deliberately not a
#: base58 checksum validation: this guards against an empty or obviously
#: wrong-chain value in config, and a half-implemented checksum would invite
#: the next reader to trust it as one.
_TRON_ADDRESS: Final[re.Pattern[str]] = re.compile(r"T[1-9A-HJ-NP-Za-km-z]{33}")


def _raw_amount(entry: dict[str, object]) -> int:
    """Read the undivided token amount out of one transfer entry.

    ``amount_str`` is preferred over ``amount``: the raw value of an 18-decimal
    token exceeds what JSON numbers carry exactly, and TronScan ships the
    string for that reason. Nothing is divided here -- decimals belong to
    :func:`app.domain.amounts.raw_to_cents`.
    """
    amount_str = entry.get("amount_str")
    if isinstance(amount_str, str) and amount_str.strip().isdigit():
        return int(amount_str.strip())

    amount = entry.get("amount")
    if isinstance(amount, int) and amount >= 0:
        return amount

    raise ValueError(f"no readable amount in {entry!r}")


class TronScanAdapter:
    """Reads one TronScan transaction record and decides what it means."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        api_url: str,
        contract_address: str,
    ) -> None:
        """Build the adapter, refusing to start misconfigured.

        No API key argument: TOR section 10 describes TronScan as public and
        keyless, and adding a key would mean adding a config entity this task
        is not authorised to add. That the description has since gone stale is
        recorded in the delivery report, not worked around here.

        Raises:
            ValueError: on a blank URL, or a contract address that is not
                shaped like a TRON base58 address. An empty contract address
                would match nothing and turn every genuine payment into
                ``not_found``; ``Settings`` accepts an empty string for it.
        """
        if not api_url.strip():
            raise ValueError("tronscan api_url must not be blank")
        if _TRON_ADDRESS.fullmatch(contract_address.strip()) is None:
            raise ValueError(f"not a TRON contract address: {contract_address!r}")

        self._client = client
        self._url = api_url.strip().rstrip("/") + "/transaction-info"
        self._contract = contract_address.strip()

    async def lookup(self, txid: str, wallet_address: str) -> ExplorerResult:
        """Fetch the transaction record for ``txid``. See the protocol."""
        if not wallet_address.strip():
            raise ValueError("wallet_address must not be blank")

        try:
            payload = await fetch_json(self._client, self._url, {"hash": txid})
        except ExplorerUnavailable:
            return ExplorerResult(verdict=Verdict.API_ERROR)

        return self._classify(payload, wallet_address.strip())

    def _classify(self, payload: object, wallet_address: str) -> ExplorerResult:
        if not isinstance(payload, dict):
            # ``[]`` and ``null`` land here. An object endpoint answering with
            # something that is not an object is a broken shape, and unlike
            # ``{}`` it says nothing about whether the transaction exists.
            return ExplorerResult(verdict=Verdict.API_ERROR)

        if not payload or not isinstance(payload.get("hash"), str):
            # ``{}`` is how TronScan spells "no such transaction": an empty
            # object of the right type, i.e. a content answer.
            return ExplorerResult(verdict=Verdict.NOT_FOUND)

        contract_ret = payload.get("contractRet")
        if isinstance(contract_ret, str) and contract_ret.upper() != "SUCCESS":
            # Reverted on chain. It exists, it moved nothing.
            return ExplorerResult(verdict=Verdict.NOT_FOUND)

        if payload.get("contractType") != TRIGGER_SMART_CONTRACT:
            return ExplorerResult(verdict=Verdict.NOT_FOUND)

        info = payload.get("trc20TransferInfo")
        if info is None:
            # A contract call that produced no TRC20 movement.
            return ExplorerResult(verdict=Verdict.NOT_FOUND)
        if not isinstance(info, list):
            return ExplorerResult(verdict=Verdict.API_ERROR)

        transfers: list[Transfer] = []
        for entry in info:
            if not isinstance(entry, dict):
                continue

            contract = entry.get("contract_address")
            if not isinstance(contract, str) or contract.strip() != self._contract:
                # Some other TRC20 token. The whole point of T-10.
                continue

            # Ours, so an unreadable field is refused rather than skipped --
            # same reasoning as the EVM adapter: skipping under-counts money.
            recipient = entry.get("to_address")
            if not isinstance(recipient, str) or not recipient.strip():
                return ExplorerResult(verdict=Verdict.API_ERROR)
            try:
                raw_amount = _raw_amount(entry)
            except ValueError:
                return ExplorerResult(verdict=Verdict.API_ERROR)

            sender = entry.get("from_address")
            transfers.append(
                Transfer(
                    recipient=recipient.strip(),
                    raw_amount=raw_amount,
                    sender=sender.strip() if isinstance(sender, str) and sender.strip() else None,
                )
            )

        return classify(transfers, wallet_address)
