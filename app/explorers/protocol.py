"""The contract every explorer adapter satisfies (T-08).

This module knows nothing about HTTP and nothing about any particular
explorer. An adapter is a function from one TXID plus one wallet address to
one *final* verdict, and that is the whole interface.

Two deliberate absences:

* **No arithmetic on the amount.** The adapter hands over ``raw_amount``
  exactly as the explorer reported it. Division lives in
  :func:`app.domain.amounts.raw_to_cents` and only there, so the floor rule of
  TOR section 6b can be wrong in one place at most.
* **No retry, no clock, no attempt counter.** The internal retry series sits
  *above* the adapter (:mod:`app.explorers.retry`). By the time a verdict
  leaves this layer the series is already over, which is why the domain has no
  "not found, but the window is still open" member.

The wallet address is a per-call argument rather than adapter state because
TOR section 4 snapshots it onto the invoice: rotating the address in config
must not break verification of invoices that were already issued, so the
caller passes the invoice's stored value, never the live setting. The USDT
contract address is the opposite -- it is not snapshotted anywhere, so it is
adapter state, checked once at construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.statuses import Verdict


@dataclass(frozen=True, slots=True)
class ExplorerResult:
    """One final verdict about one TXID, plus what only the adapter can know.

    ``verdict`` is typed as the whole seven-member :class:`Verdict` rather than
    a narrower enum of its own: the vocabulary belongs to the domain and
    forking it here would create a second place to add an eighth member. What
    an adapter can actually produce is four of the seven:

    * ``matched``, ``not_found``, ``wrong_address`` -- content answers;
    * ``api_error`` -- the explorer did not give a content answer at all.

    The other three are produced elsewhere by construction. ``invalid_format``
    comes from the regex gate before any adapter is built. ``already_used`` is
    a verdict of the partial unique index, which an adapter cannot see.
    ``wrong_network`` is produced by nobody today: the format gate rejects a
    foreign-network TXID as ``invalid_format`` and the two EVM networks share
    one format, so telling them apart would need a cross-chain call that no
    task authorises. That is a live question for the owner, not an oversight --
    see the delivery report.

    ``from_address`` is the *Transfer sender*, not the account that submitted
    the transaction. The two differ whenever a contract moves tokens on
    someone's behalf, and ``invoice_txid_attempts.from_address`` is about who
    paid. It is filled whenever a Transfer of our contract was parsed, which
    includes the ``wrong_address`` case, and left empty when nothing was
    parsed.
    """

    verdict: Verdict
    raw_amount: int | None = None
    from_address: str | None = None

    def __post_init__(self) -> None:
        # Money, so it is a contract rather than a convention: an amount that
        # travels with a non-match would be read by somebody eventually, and a
        # match without one would credit nothing.
        if self.verdict is Verdict.MATCHED:
            if self.raw_amount is None:
                raise ValueError("matched requires raw_amount")
            if self.raw_amount < 0:
                raise ValueError(f"raw_amount must be non-negative, got {self.raw_amount}")
        elif self.raw_amount is not None:
            raise ValueError(f"{self.verdict} must not carry raw_amount")

        # Nothing was parsed on these two, so a sender here would be invented.
        parsed_nothing = self.verdict in (Verdict.API_ERROR, Verdict.INVALID_FORMAT)
        if parsed_nothing and self.from_address is not None:
            raise ValueError(f"{self.verdict} must not carry from_address")


class ExplorerAdapter(Protocol):
    """What :mod:`app.explorers.retry` and :mod:`app.explorers.verify` require."""

    async def lookup(self, txid: str, wallet_address: str) -> ExplorerResult:
        """Ask the explorer about ``txid`` once and classify the answer.

        One call, one answer, no retry: retrying is the loop's job.

        Raises:
            ValueError: if ``wallet_address`` is blank. An invoice with no
                stored address is corrupt data, not a domain state, and
                comparing against an empty string would quietly turn every
                payment into ``not_found``.
        """
        ...
