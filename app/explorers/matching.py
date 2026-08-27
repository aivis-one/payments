"""The rule of TOR section 6a, written once for both chains.

Etherscan and TronScan disagree about everything on the wire -- envelopes,
address encoding, where the amount lives -- but they do not disagree about
what counts as a payment. Keeping that decision in one function is what stops
the two adapters from drifting apart, which is the shape the legacy bug had:
the TRON path checked the issuing contract and the EVM path did not.

The input is already filtered: each adapter passes only the transfers emitted
by *our* USDT contract. Everything else -- a foreign token moving to our
address, an ``Approval``, a plain native transfer -- is dropped before it gets
here, which is why "no transfers at all" and "no transfers of ours" collapse
into the same ``not_found`` and need no separate branch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.domain.statuses import Verdict
from app.explorers.protocol import ExplorerResult

#: The Transfer(address,address,uint256) event signature. Checked because our
#: contract emits other events too -- an ``Approval`` from the USDT contract
#: to our address is not a payment.
TRANSFER_TOPIC: Final[str] = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


@dataclass(frozen=True, slots=True)
class Transfer:
    """One Transfer of our USDT contract, as the explorer reported it.

    ``raw_amount`` is the on-chain integer, undivided. ``sender`` is optional
    because an explorer may omit it; the recipient is not, because without it
    there is nothing to decide.
    """

    recipient: str
    raw_amount: int
    sender: str | None = None


def classify(transfers: list[Transfer], wallet_address: str) -> ExplorerResult:
    """Turn the transfers of our contract into one verdict.

    * none at all -> ``not_found``. TOR section 4 is explicit that a transfer
      of some other token to our address is ``not_found`` and gets no code of
      its own: the network was right, there was simply no valid USDT payment
      in the transaction.
    * some, but none to us -> ``wrong_address``. Somebody's real USDT payment,
      sent somewhere else.
    * one or more to us -> ``matched``, and **all of them are summed**. A
      sending wallet may split one payment across several Transfer events in a
      single transaction, and crediting only the first would lose the rest.

    The sender reported on a match is the sender of the first matching
    transfer. A split payment funded from several accounts therefore records
    one of them; ``invoice_txid_attempts.from_address`` is a single nullable
    column, so this is the model's shape rather than a gap in the parse. Not
    marked as a ceiling: it is a consequence of an owner-level data model
    decision, not a deferred fix of ours.
    """
    if not transfers:
        return ExplorerResult(verdict=Verdict.NOT_FOUND)

    ours = [transfer for transfer in transfers if transfer.recipient == wallet_address]
    if not ours:
        return ExplorerResult(
            verdict=Verdict.WRONG_ADDRESS,
            from_address=transfers[0].sender,
        )

    return ExplorerResult(
        verdict=Verdict.MATCHED,
        raw_amount=sum(transfer.raw_amount for transfer in ours),
        from_address=ours[0].sender,
    )
