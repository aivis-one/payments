"""The invoice state machine as one pure function.

``decide()`` reads a snapshot, an event, ``now`` and a policy, and returns a
:class:`Decision`. It touches no database, no network and no system clock:
``now`` is a parameter precisely so that time-dependent transitions can be
resolved lazily by whoever is calling, without a background timer.

It is total over all six persistent statuses for all four events. Every cell of
those four tables is listed explicitly in the handlers below; a status that is
not in a table is a status nobody thought about.

Two conventions that H3 must not re-implement:

* ``Decision.refused_by`` carries the *status* that caused the refusal, never
  an HTTP error string. The 409 table of TOR section 8 is keyed one-to-one by
  status, so mapping it is H3's job and this module holds no copy of it.
* A refusal can carry a status change. ``TxidAdmission`` against an invoice
  whose TTL has passed returns refusal *and* ``next_status = expired`` in the
  same decision. A caller that reads "refused" as "answer 409 and write
  nothing" loses the resolve: the invoice stays ``created`` past its TTL until
  something else touches it, and the product never receives the ``expired``
  event of TOR section 8. Persist ``next_status`` on refusals too.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import assert_never

from app.domain.amounts import raw_to_cents
from app.domain.events import (
    ConfirmationsObserved,
    Event,
    InvoiceSnapshot,
    TimeChecked,
    TxidAdmission,
    TxidVerdict,
    require_aware,
)
from app.domain.policy import Policy
from app.domain.statuses import AttemptResultCode, InvoiceStatus, Verdict

#: Verdicts that become a row in ``invoice_txid_attempts``.
#:
#: ``api_error`` is absent because an unreachable explorer is an infrastructure
#: failure, not a user attempt (TOR sections 4 and 7). ``invalid_format`` is
#: absent because the regex rejects it before any explorer call.
_ATTEMPT_RECORD_BY_VERDICT: dict[Verdict, AttemptResultCode] = {
    Verdict.MATCHED: AttemptResultCode.MATCHED,
    Verdict.NOT_FOUND: AttemptResultCode.NOT_FOUND,
    Verdict.WRONG_ADDRESS: AttemptResultCode.WRONG_ADDRESS,
    Verdict.WRONG_NETWORK: AttemptResultCode.WRONG_NETWORK,
    Verdict.ALREADY_USED: AttemptResultCode.ALREADY_USED,
}


@dataclass(frozen=True, slots=True)
class Effects:
    """Field-level consequences of a transition.

    Returned by the function rather than recomputed by the caller: the credited
    amount and the underpaid flag are money, and money must be computed once.
    ``attempt_record`` on :class:`Decision` is the same argument for the "does
    this cost an attempt" rule.
    """

    credited_amount_cents: int | None = None
    underpaid: bool | None = None
    slot_frozen_at: datetime | None = None
    active_txid: str | None = None
    attempts_used_delta: int = 0


NO_EFFECTS = Effects()


@dataclass(frozen=True, slots=True)
class Decision:
    """What the caller must persist and answer.

    There is no ``status_changed`` flag: it is ``next_status != current``, and a
    second field carrying a derived truth is a second place to get it wrong.
    """

    next_status: InvoiceStatus
    refused_by: InvoiceStatus | None = None
    effects: Effects = NO_EFFECTS
    attempt_record: AttemptResultCode | None = None

    @property
    def accepted(self) -> bool:
        """True when the event was processed rather than refused."""
        return self.refused_by is None


def decide(
    snapshot: InvoiceSnapshot,
    event: Event,
    now: datetime,
    policy: Policy,
) -> Decision:
    """Return the decision for ``event`` against ``snapshot`` at ``now``."""
    require_aware(now, "now")

    match event:
        case TxidAdmission():
            return _on_admission(snapshot, now, policy)
        case TxidVerdict():
            return _on_verdict(snapshot, event, now, policy)
        case ConfirmationsObserved():
            return _on_confirmations(snapshot, event, now, policy)
        case TimeChecked():
            return Decision(next_status=_resolve_by_time(snapshot, now, policy))
        case _:
            # Type-level exhaustiveness only: mypy fails here if a fifth event
            # is added without a handler. Not a runtime state.
            assert_never(event)


def _resolve_by_time(snapshot: InvoiceSnapshot, now: datetime, policy: Policy) -> InvoiceStatus:
    """Resolve time-dependent transitions lazily. Table 4 of the plan.

    Boundaries are inclusive on both deadlines (``>=``): a deadline fires at
    the moment it is reached, not a microsecond later.
    """
    match snapshot.status:
        case InvoiceStatus.CREATED:
            return InvoiceStatus.EXPIRED if now >= snapshot.expires_at else InvoiceStatus.CREATED

        case InvoiceStatus.ATTEMPTS_EXHAUSTED:
            # Not terminal: it accepts no new TXID but still waits for its TTL.
            return (
                InvoiceStatus.EXPIRED
                if now >= snapshot.expires_at
                else InvoiceStatus.ATTEMPTS_EXHAUSTED
            )

        case InvoiceStatus.AWAITING_CONFIRMATIONS:
            # From slot_frozen_at onwards the TTL stops being grounds for
            # expiry (TOR section 11 p.1, an owner decision, not a deferred
            # gap): a transaction that is already on-chain must not be thrown
            # away because a timer designed for "user never paid" ran out. The
            # upper bound on waiting is MAX_OBSERVATION_WINDOW instead, which
            # is why expires_at is not consulted at all in this branch.
            frozen_at = snapshot.slot_frozen_at
            assert frozen_at is not None  # invariant of InvoiceSnapshot
            if now - frozen_at >= policy.max_observation_window:
                return InvoiceStatus.STALLED
            return InvoiceStatus.AWAITING_CONFIRMATIONS

        case InvoiceStatus.CONFIRMED | InvoiceStatus.EXPIRED | InvoiceStatus.STALLED:
            return snapshot.status

        case _:
            assert_never(snapshot.status)


def _on_admission(snapshot: InvoiceSnapshot, now: datetime, policy: Policy) -> Decision:
    """Table 1: a TXID arrives, before format check and before the explorer.

    No attempt is ever spent here -- refused calls never reach the explorer, so
    there is physically nothing to charge for.
    """
    resolved = _resolve_by_time(snapshot, now, policy)
    if resolved is not InvoiceStatus.CREATED:
        # Covers the five non-accepting statuses and the two lazy resolves
        # (created -> expired, awaiting_confirmations -> stalled). When both a
        # resolve and an exhausted budget apply, expiry wins: it is terminal
        # and it is what would have happened anyway.
        return Decision(next_status=resolved, refused_by=resolved)

    if snapshot.attempts_used >= policy.max_txid_attempts:
        # Reachable only because the budget is configurable: lowering
        # MAX_TXID_ATTEMPTS leaves live invoices whose attempts_used is at or
        # above the new ceiling. Resolving here refuses before the explorer
        # call, so the user is not charged for a budget that shrank under them.
        return Decision(
            next_status=InvoiceStatus.ATTEMPTS_EXHAUSTED,
            refused_by=InvoiceStatus.ATTEMPTS_EXHAUSTED,
        )

    return Decision(next_status=InvoiceStatus.CREATED)


def _on_verdict(
    snapshot: InvoiceSnapshot,
    event: TxidVerdict,
    now: datetime,
    policy: Policy,
) -> Decision:
    """Table 2: the explorer layer answered.

    Time is deliberately NOT re-resolved here. Up to about seven seconds of
    internal retry can pass between admission and verdict; re-checking the TTL
    would throw away a payment that has just been found on-chain. Same
    precedence as the confirmed/stalled tie-break below: a payment found in the
    network beats an expired timer.
    """
    if snapshot.status is not InvoiceStatus.CREATED:
        # A verdict arriving for a non-created invoice is a race that H3 closes
        # by re-reading inside the transaction. The function answers it instead
        # of raising.
        return Decision(next_status=snapshot.status, refused_by=snapshot.status)

    match event.verdict:
        case Verdict.API_ERROR | Verdict.INVALID_FORMAT:
            # No row, no attempt, no status change. Infrastructure failure and
            # malformed input are both on the service's side of the line.
            return Decision(next_status=InvoiceStatus.CREATED)

        case Verdict.MATCHED:
            # The budget is intentionally not re-checked after this increment:
            # a valid payment arriving on the last allowed attempt must occupy
            # the slot, not drown in attempts_exhausted.
            return Decision(
                next_status=InvoiceStatus.AWAITING_CONFIRMATIONS,
                effects=Effects(
                    slot_frozen_at=now,
                    active_txid=event.txid,
                    attempts_used_delta=1,
                ),
                attempt_record=AttemptResultCode.MATCHED,
            )

        case (
            Verdict.NOT_FOUND
            | Verdict.WRONG_ADDRESS
            | Verdict.WRONG_NETWORK
            | Verdict.ALREADY_USED
        ):
            # Increment first, then compare -- and compare with >=, not ==.
            # The budget lives in the environment while invoices outlive
            # restarts, so attempts_used can already sit above the ceiling;
            # equality would leave that case matching neither branch and the
            # function would stop being total.
            used_after = snapshot.attempts_used + 1
            next_status = (
                InvoiceStatus.ATTEMPTS_EXHAUSTED
                if used_after >= policy.max_txid_attempts
                else InvoiceStatus.CREATED
            )
            return Decision(
                next_status=next_status,
                effects=Effects(attempts_used_delta=1),
                attempt_record=_ATTEMPT_RECORD_BY_VERDICT[event.verdict],
            )

        case _:
            assert_never(event.verdict)


def _on_confirmations(
    snapshot: InvoiceSnapshot,
    event: ConfirmationsObserved,
    now: datetime,
    policy: Policy,
) -> Decision:
    """Table 3: one observation by the confirmations worker.

    Three outcomes of one event: threshold reached -> confirmed, window
    expired -> stalled, neither -> unchanged.
    """
    if snapshot.status is not InvoiceStatus.AWAITING_CONFIRMATIONS:
        return Decision(next_status=snapshot.status, refused_by=snapshot.status)

    if event.confirmations >= policy.confirmations_required:
        # Tie-break, checked first on purpose: an observation can carry both
        # "threshold reached" and "observation window already expired" when the
        # worker did not get here in time. Confirmed wins -- refusing a payment
        # that is demonstrably in the network because of an elapsed timer is
        # worse than holding the invoice longer (same precedence as TOR
        # section 11 p.1).
        credited = raw_to_cents(event.raw_amount, policy.decimals)

        # KNOWN CEILING: a chain reorg after this transition is not undone.
        #
        # Mechanics: once the invoice enters ``confirmed`` the service has
        #   already told the product to credit the balance. If the block
        #   carrying the transaction is later displaced by a longer chain, the
        #   money is gone from the chain but the credit stands -- neither this
        #   service nor the TOR provides a rollback path.
        # Status: acknowledged by design.
        # Task: none. At the CONFIRMATIONS_REQUIRED levels of TOR section 10
        #   the owner judged the probability negligible for MVP and explicitly
        #   declined to open a task for it.
        # Unfreeze trigger: an actual, observed case of a confirmed payment
        #   being rolled back in production -- not a hypothetical one.
        # Agreed fix shape: a chargeback path for crypto as a separate line of
        #   work (today explicitly out of scope, TOR section 2), not a patch on
        #   this function.
        # Rejected: raising CONFIRMATIONS_REQUIRED by an order of magnitude
        #   pre-emptively -- rejected as a disproportionate delay to every user
        #   against a negligible risk.

        # Overpayment is not trimmed and underpayment is not blocking: both
        # credit exactly what arrived (TOR section 6).
        return Decision(
            next_status=InvoiceStatus.CONFIRMED,
            effects=Effects(
                credited_amount_cents=credited,
                underpaid=credited < snapshot.invoice_amount_cents,
            ),
        )

    frozen_at = snapshot.slot_frozen_at
    assert frozen_at is not None  # invariant of InvoiceSnapshot
    if now - frozen_at >= policy.max_observation_window:
        # Terminal. The service never reopens a stalled invoice: if the money
        # does arrive later, that is a staff matter, not an automatic one.
        return Decision(next_status=InvoiceStatus.STALLED)

    return Decision(next_status=InvoiceStatus.AWAITING_CONFIRMATIONS)
