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

    ``verdict`` is typed as the whole six-member :class:`Verdict` rather than
    a narrower enum of its own: the vocabulary belongs to the domain and
    forking it here would create a second place to add a seventh member. What
    an adapter can actually produce is four of the six:

    * ``matched``, ``not_found``, ``wrong_address`` -- content answers;
    * ``api_error`` -- the explorer did not give a content answer at all.

    The other two are produced elsewhere by construction. ``invalid_format``
    comes from the regex gate before any adapter is built: a TXID that does not
    match its own network's shape is refused for free, before any call, so it
    never reaches an adapter to be classified. ``already_used`` is decided at
    the INSERT against the partial unique index -- the explorer cannot know
    that some other invoice already holds this TXID, so no adapter can produce
    it either.

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


@dataclass(frozen=True, slots=True)
class ExplorerObservation:
    """One look at a transaction that is already holding an invoice's slot.

    Composition rather than an extra field on :class:`ExplorerResult`. The
    submission path never needs a confirmation depth, and giving it a field
    that is always ``None`` there would be a lie about when it is populated --
    besides which computing depth costs a second call on EVM, which that path
    must not pay for (TOR section 7: it is synchronous and already carries up
    to seven seconds of retry).

    ``raw_amount`` is read again here rather than carried over from the match.
    TOR section 6 fixes the credited amount at the moment ``confirmations >=
    N``, not at match time, which is also why ``ConfirmationsObserved`` asks
    for both numbers together and why this is one call rather than two.
    """

    result: ExplorerResult
    confirmations: int | None = None

    def __post_init__(self) -> None:
        # A depth is only meaningful for a transaction we recognised. If a
        # re-observation comes back not_found -- a reorg displaced it, say --
        # there is no depth to report, and inventing zero would read as "seen,
        # not yet confirmed" instead of "no longer there".
        if self.result.verdict is Verdict.MATCHED:
            if self.confirmations is None:
                raise ValueError("a matched observation must carry confirmations")
            if self.confirmations < 0:
                raise ValueError(f"confirmations must be non-negative, got {self.confirmations}")
        elif self.confirmations is not None:
            raise ValueError(f"{self.result.verdict} must not carry confirmations")


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

    async def observe(self, txid: str, wallet_address: str) -> ExplorerObservation:
        """Re-read the transaction and report how deeply it is buried.

        Used only by the confirmations worker. Whether this costs one call or
        two is the adapter's business: TRON reports depth in the same response
        that carries the transfer, EVM needs the chain head as well.

        No retry here. The retry series exists because a freshly submitted hash
        may not be indexed yet; a transaction that already took an invoice's
        slot was indexed once, and if it has gone missing the worker wants to
        know now rather than seven seconds from now.
        """
        ...
