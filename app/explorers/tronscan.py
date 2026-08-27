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

from typing import Final

import httpx

from app.domain.addresses import TRON_ADDRESS
from app.domain.statuses import Verdict
from app.explorers.matching import Transfer, classify
from app.explorers.protocol import ExplorerObservation, ExplorerResult
from app.explorers.transport import ExplorerUnavailable, fetch_json

#: TronScan's API key header (TOR section 10). Sent on every request: an
#: unkeyed caller is rate-limited rather than refused, so the failure this
#: prevents is intermittent under load rather than obvious at startup.
API_KEY_HEADER: Final[str] = "TRON-PRO-API-KEY"

#: ``TriggerSmartContract``. Any contract call, token or not.
TRIGGER_SMART_CONTRACT: Final[int] = 31



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
        api_key: str,
        contract_address: str,
    ) -> None:
        """Build the adapter, refusing to start misconfigured.

        The API key is checked the same way Etherscan's is, and for the same
        reason: a blank one is not refused by the explorer, it is silently
        throttled. That failure surfaces weeks later as intermittent
        ``api_error`` under load rather than as a service that will not start,
        and ``Settings`` accepts ``""`` for a mandatory ``str`` field.

        Raises:
            ValueError: on a blank URL or key, or a contract address that is
                not shaped like a TRON base58 address. An empty contract
                address would match nothing and turn every genuine payment
                into ``not_found``.
        """
        if not api_url.strip():
            raise ValueError("tronscan api_url must not be blank")
        if not api_key.strip():
            raise ValueError("tronscan api_key must not be blank")
        if TRON_ADDRESS.fullmatch(contract_address.strip()) is None:
            raise ValueError(f"not a TRON contract address: {contract_address!r}")

        self._client = client
        self._url = api_url.strip().rstrip("/") + "/transaction-info"
        self._headers = {API_KEY_HEADER: api_key.strip()}
        self._contract = contract_address.strip()

    async def lookup(self, txid: str, wallet_address: str) -> ExplorerResult:
        """Fetch the transaction record for ``txid``. See the protocol."""
        if not wallet_address.strip():
            raise ValueError("wallet_address must not be blank")

        try:
            payload = await fetch_json(
                self._client, self._url, {"hash": txid}, headers=self._headers
            )
        except ExplorerUnavailable:
            return ExplorerResult(verdict=Verdict.API_ERROR)

        return self._classify(payload, wallet_address.strip())

    async def observe(self, txid: str, wallet_address: str) -> ExplorerObservation:
        """One call. TronScan reports the depth in the same record as the transfer.

        The asymmetry with the EVM adapter is the chain's, not a design choice:
        TRON's transaction record carries ``confirmations`` outright, while an
        Ethereum receipt carries only the block it landed in and has to be
        subtracted from the chain head.

        The reported number is taken as TronScan states it. The two chains'
        conventions may differ by one block, and the EVM side is deliberately
        counted conservatively for that reason; here there is nothing to
        choose, and inventing an adjustment would be guessing at somebody
        else's definition.
        """
        if not wallet_address.strip():
            raise ValueError("wallet_address must not be blank")

        try:
            payload = await fetch_json(
                self._client, self._url, {"hash": txid}, headers=self._headers
            )
        except ExplorerUnavailable:
            return ExplorerObservation(result=ExplorerResult(verdict=Verdict.API_ERROR))

        result = self._classify(payload, wallet_address.strip())
        if result.verdict is not Verdict.MATCHED:
            return ExplorerObservation(result=result)

        depth = payload.get("confirmations") if isinstance(payload, dict) else None
        if not isinstance(depth, int) or isinstance(depth, bool) or depth < 0:
            # Matched but undatable. Reporting zero would say "seen, not yet
            # confirmed" about a transaction whose depth we simply failed to
            # read, and the worker would wait on it forever.
            return ExplorerObservation(result=ExplorerResult(verdict=Verdict.API_ERROR))

        return ExplorerObservation(result=result, confirmations=depth)

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
