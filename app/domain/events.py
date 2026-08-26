"""Events the transition function reacts to, and the invoice snapshot it reads.

Four events, not two. The interval between "a TXID was handed to us" and "the
explorer answered" is physically real -- the explorer call happens outside the
database transaction -- so it is expressed as two events rather than as a
persisted ``submitted`` status:

* :class:`TxidAdmission` -- before format validation and before the explorer.
  This is the guard of TOR section 8: five statuses are refused here, and no
  attempt is spent because the call never reaches the explorer.
* :class:`TxidVerdict` -- after the explorer adapter (H2) returned a final
  verdict.
* :class:`ConfirmationsObserved` -- the confirmations worker (H4). Carries
  ``raw_amount`` because TOR section 6 fixes the credited amount at the moment
  ``confirmations >= N``, not at match time.
* :class:`TimeChecked` -- the lazy resolve of time-dependent transitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.domain.statuses import InvoiceStatus, Verdict


def require_aware(moment: datetime, field: str) -> None:
    """Reject naive datetimes.

    Comparing naive against aware raises ``TypeError`` from inside an operator,
    which would be an untotal branch hidden in a comparison. The check is done
    where the value enters the domain instead.
    """
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware (UTC), got naive {moment!r}")


@dataclass(frozen=True, slots=True)
class TxidAdmission:
    """A TXID was submitted; nothing has been validated or looked up yet."""

    txid: str


@dataclass(frozen=True, slots=True)
class TxidVerdict:
    """The explorer layer reached a final verdict on a submitted TXID.

    Named for the TXID, not for the explorer: ``INVALID_FORMAT`` never reaches
    an explorer at all, and an event class called ``ExplorerVerdict`` would lie
    about that member to the next reader.
    """

    verdict: Verdict
    txid: str


@dataclass(frozen=True, slots=True)
class ConfirmationsObserved:
    """One observation of an in-flight transaction by the confirmations worker.

    One event with three outcomes -- confirmed, stalled, or unchanged -- rather
    than a separate "observation window expired" event. Both exits from
    ``awaiting_confirmations`` are reactions to the same observation; splitting
    them would create two places deciding one status.
    """

    confirmations: int
    raw_amount: int


@dataclass(frozen=True, slots=True)
class TimeChecked:
    """Nothing happened except the passage of time."""


Event = TxidAdmission | TxidVerdict | ConfirmationsObserved | TimeChecked


@dataclass(frozen=True, slots=True)
class InvoiceSnapshot:
    """The invoice fields the transition function is allowed to read.

    Deliberately not the ORM row: the function must not be able to lazy-load,
    refresh, or write anything.
    """

    status: InvoiceStatus
    invoice_amount_cents: int
    attempts_used: int
    expires_at: datetime
    slot_frozen_at: datetime | None = None

    def __post_init__(self) -> None:
        require_aware(self.expires_at, "expires_at")
        if self.slot_frozen_at is not None:
            require_aware(self.slot_frozen_at, "slot_frozen_at")
        if self.attempts_used < 0:
            raise ValueError(f"attempts_used must be non-negative, got {self.attempts_used}")
        # Invariant, not a branch: an invoice in awaiting_confirmations always
        # froze its slot on the way in. Guarding it here keeps the transition
        # function free of a fallback branch for a row that cannot exist.
        if self.status is InvoiceStatus.AWAITING_CONFIRMATIONS and self.slot_frozen_at is None:
            raise ValueError("awaiting_confirmations requires slot_frozen_at")

    # No lower bound on invoice_amount_cents by design. There is no CHECK
    # constraint on the column either (T-03 fence), so zero is genuinely
    # reachable here and the emptiness test that feeds zero is honest. If a
    # CHECK is ever added in H3, that same test starts building an unreachable
    # value by hand and must be re-pointed at the request schema instead.
