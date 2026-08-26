"""ConfirmationsObserved: one event, three outcomes (table 3)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.domain.events import ConfirmationsObserved
from app.domain.statuses import InvoiceStatus
from app.domain.transitions import decide
from tests.conftest import NOW, POLICY_6, POLICY_18, snapshot

FRESH = NOW - timedelta(minutes=5)
STALE = NOW - timedelta(days=8)


def test_threshold_reached_confirms_and_credits():
    decision = decide(
        snapshot(InvoiceStatus.AWAITING_CONFIRMATIONS, invoice_amount_cents=10_000),
        ConfirmationsObserved(confirmations=12, raw_amount=100_000_000),
        NOW,
        POLICY_6,
    )

    assert decision.next_status is InvoiceStatus.CONFIRMED
    assert decision.effects.credited_amount_cents == 10_000
    assert decision.effects.underpaid is False


def test_threshold_is_inclusive():
    """confirmations == N confirms; the boundary fires when it is reached."""
    decision = decide(
        snapshot(InvoiceStatus.AWAITING_CONFIRMATIONS),
        ConfirmationsObserved(confirmations=POLICY_6.confirmations_required, raw_amount=1),
        NOW,
        POLICY_6,
    )

    assert decision.next_status is InvoiceStatus.CONFIRMED


def test_underpayment_confirms_and_flags():
    decision = decide(
        snapshot(InvoiceStatus.AWAITING_CONFIRMATIONS, invoice_amount_cents=10_000),
        ConfirmationsObserved(confirmations=20, raw_amount=99_000_000),
        NOW,
        POLICY_6,
    )

    assert decision.next_status is InvoiceStatus.CONFIRMED
    assert decision.effects.credited_amount_cents == 9_900
    assert decision.effects.underpaid is True


def test_overpayment_is_not_trimmed():
    """The product is told to credit what arrived, not what was invoiced."""
    decision = decide(
        snapshot(InvoiceStatus.AWAITING_CONFIRMATIONS, invoice_amount_cents=10_000),
        ConfirmationsObserved(confirmations=20, raw_amount=150_000_000),
        NOW,
        POLICY_6,
    )

    assert decision.effects.credited_amount_cents == 15_000
    assert decision.effects.underpaid is False


def test_dust_transfer_confirms_with_zero_credit():
    """The limit case of an already accepted rule, not a special case.

    A sub-cent transfer from the right contract is a valid USDT transfer. TOR
    section 6 credits any shortfall as it stands, and a cut-off threshold is a
    business decision that does not exist yet -- inventing one in the adapter
    would lie about the state of the chain and burn a user attempt.
    """
    decision = decide(
        snapshot(InvoiceStatus.AWAITING_CONFIRMATIONS, invoice_amount_cents=10_000),
        ConfirmationsObserved(confirmations=20, raw_amount=9_000),
        NOW,
        POLICY_6,
    )

    assert decision.next_status is InvoiceStatus.CONFIRMED
    assert decision.effects.credited_amount_cents == 0
    assert decision.effects.underpaid is True


def test_below_threshold_inside_window_keeps_waiting():
    decision = decide(
        snapshot(InvoiceStatus.AWAITING_CONFIRMATIONS, slot_frozen_at=FRESH),
        ConfirmationsObserved(confirmations=3, raw_amount=100_000_000),
        NOW,
        POLICY_6,
    )

    assert decision.next_status is InvoiceStatus.AWAITING_CONFIRMATIONS
    assert decision.effects.credited_amount_cents is None


def test_below_threshold_past_window_stalls():
    decision = decide(
        snapshot(InvoiceStatus.AWAITING_CONFIRMATIONS, slot_frozen_at=STALE),
        ConfirmationsObserved(confirmations=3, raw_amount=100_000_000),
        NOW,
        POLICY_6,
    )

    assert decision.next_status is InvoiceStatus.STALLED
    assert decision.effects.credited_amount_cents is None


def test_window_boundary_is_inclusive():
    frozen = NOW - POLICY_6.max_observation_window
    decision = decide(
        snapshot(InvoiceStatus.AWAITING_CONFIRMATIONS, slot_frozen_at=frozen),
        ConfirmationsObserved(confirmations=3, raw_amount=1),
        NOW,
        POLICY_6,
    )

    assert decision.next_status is InvoiceStatus.STALLED


def test_confirmed_wins_the_tie_against_an_expired_window():
    """Both conditions in one observation; the worker may simply be late.

    Refusing a payment that is demonstrably in the network because a timer
    elapsed is worse than holding the invoice longer.
    """
    decision = decide(
        snapshot(InvoiceStatus.AWAITING_CONFIRMATIONS, slot_frozen_at=STALE),
        ConfirmationsObserved(confirmations=99, raw_amount=100_000_000),
        NOW,
        POLICY_6,
    )

    assert decision.next_status is InvoiceStatus.CONFIRMED
    assert decision.effects.credited_amount_cents == 10_000


def test_ttl_does_not_apply_after_the_slot_is_frozen():
    """Long past expires_at, an in-flight transaction still confirms."""
    decision = decide(
        snapshot(InvoiceStatus.AWAITING_CONFIRMATIONS, slot_frozen_at=FRESH),
        ConfirmationsObserved(confirmations=20, raw_amount=100_000_000),
        NOW + timedelta(days=3),
        POLICY_6,
    )

    assert decision.next_status is InvoiceStatus.CONFIRMED


def test_bsc_18_decimals_credits_the_same_as_erc20_6_decimals_would_not():
    """Same raw number, different networks, different money."""
    raw = 10**18
    event = ConfirmationsObserved(confirmations=20, raw_amount=raw)

    bsc = decide(snapshot(InvoiceStatus.AWAITING_CONFIRMATIONS), event, NOW, POLICY_18)
    erc = decide(snapshot(InvoiceStatus.AWAITING_CONFIRMATIONS), event, NOW, POLICY_6)

    assert bsc.effects.credited_amount_cents == 100
    assert erc.effects.credited_amount_cents == 100_000_000_000_000


@pytest.mark.parametrize(
    "status",
    [
        InvoiceStatus.CREATED,
        InvoiceStatus.ATTEMPTS_EXHAUSTED,
        InvoiceStatus.CONFIRMED,
        InvoiceStatus.EXPIRED,
        InvoiceStatus.STALLED,
    ],
)
def test_observation_of_a_non_awaiting_invoice_is_refused(status: InvoiceStatus):
    decision = decide(
        snapshot(status), ConfirmationsObserved(confirmations=99, raw_amount=10**9), NOW, POLICY_6
    )

    assert decision.refused_by is status
    assert decision.next_status is status
    assert decision.effects.credited_amount_cents is None


def test_confirmed_is_never_recredited():
    """A second observation carrying a different amount changes nothing."""
    decision = decide(
        snapshot(InvoiceStatus.CONFIRMED),
        ConfirmationsObserved(confirmations=99, raw_amount=999_999_999),
        NOW,
        POLICY_6,
    )

    assert decision.effects.credited_amount_cents is None
    assert decision.effects.underpaid is None


def test_confirmations_is_total_over_all_six_statuses():
    for status in InvoiceStatus:
        decide(
            snapshot(status), ConfirmationsObserved(confirmations=0, raw_amount=0), NOW, POLICY_6
        )
