"""TimeChecked and the snapshot invariants (table 4)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.events import InvoiceSnapshot, TimeChecked, TxidAdmission
from app.domain.statuses import InvoiceStatus
from app.domain.transitions import decide
from tests.conftest import EXPIRES_AT, NOW, POLICY_6, snapshot

TICK = TimeChecked()


def test_created_before_ttl_is_unchanged():
    decision = decide(snapshot(InvoiceStatus.CREATED), TICK, NOW, POLICY_6)

    assert decision.next_status is InvoiceStatus.CREATED
    assert decision.accepted


def test_created_at_ttl_expires():
    """The deadline fires at the moment it is reached, not after it."""
    decision = decide(snapshot(InvoiceStatus.CREATED), TICK, EXPIRES_AT, POLICY_6)

    assert decision.next_status is InvoiceStatus.EXPIRED


def test_attempts_exhausted_expires_on_ttl():
    """attempts_exhausted is not terminal: it still waits for its TTL."""
    decision = decide(snapshot(InvoiceStatus.ATTEMPTS_EXHAUSTED), TICK, EXPIRES_AT, POLICY_6)

    assert decision.next_status is InvoiceStatus.EXPIRED


def test_attempts_exhausted_before_ttl_is_unchanged():
    decision = decide(snapshot(InvoiceStatus.ATTEMPTS_EXHAUSTED), TICK, NOW, POLICY_6)

    assert decision.next_status is InvoiceStatus.ATTEMPTS_EXHAUSTED


def test_awaiting_confirmations_ignores_the_ttl():
    """From slot_frozen_at onwards the TTL is not grounds for expiry."""
    decision = decide(
        snapshot(InvoiceStatus.AWAITING_CONFIRMATIONS, slot_frozen_at=NOW - timedelta(hours=1)),
        TICK,
        EXPIRES_AT + timedelta(days=2),
        POLICY_6,
    )

    assert decision.next_status is InvoiceStatus.AWAITING_CONFIRMATIONS


def test_awaiting_confirmations_stalls_past_the_observation_window():
    decision = decide(
        snapshot(InvoiceStatus.AWAITING_CONFIRMATIONS, slot_frozen_at=NOW - timedelta(days=8)),
        TICK,
        NOW,
        POLICY_6,
    )

    assert decision.next_status is InvoiceStatus.STALLED


@pytest.mark.parametrize(
    "status", [InvoiceStatus.CONFIRMED, InvoiceStatus.EXPIRED, InvoiceStatus.STALLED]
)
def test_terminal_statuses_are_untouched_by_any_amount_of_time(status: InvoiceStatus):
    decision = decide(snapshot(status), TICK, NOW + timedelta(days=3650), POLICY_6)

    assert decision.next_status is status
    assert decision.accepted


def test_time_check_is_idempotent_on_already_resolved_invoices():
    once = decide(snapshot(InvoiceStatus.CREATED), TICK, EXPIRES_AT, POLICY_6)
    twice = decide(snapshot(once.next_status), TICK, EXPIRES_AT, POLICY_6)

    assert once.next_status is twice.next_status is InvoiceStatus.EXPIRED


def test_time_check_is_total_over_all_six_statuses():
    for status in InvoiceStatus:
        decide(snapshot(status), TICK, NOW, POLICY_6)


def test_naive_now_is_rejected_before_any_comparison():
    """Naive vs aware raises TypeError from inside an operator otherwise.

    That would be an untotal branch hidden in a comparison, so the check moved
    to the boundary where the value enters the domain.
    """
    with pytest.raises(ValueError, match="timezone-aware"):
        decide(snapshot(InvoiceStatus.CREATED), TICK, datetime(2026, 8, 26, 12, 0), POLICY_6)


def test_naive_expires_at_is_rejected_at_construction():
    with pytest.raises(ValueError, match="timezone-aware"):
        InvoiceSnapshot(
            status=InvoiceStatus.CREATED,
            invoice_amount_cents=1,
            attempts_used=0,
            expires_at=datetime(2026, 8, 26, 13, 0),
        )


def test_awaiting_confirmations_without_a_frozen_slot_cannot_be_built():
    """An invariant, not a branch: the row cannot exist, so it is not handled."""
    with pytest.raises(ValueError, match="slot_frozen_at"):
        InvoiceSnapshot(
            status=InvoiceStatus.AWAITING_CONFIRMATIONS,
            invoice_amount_cents=1,
            attempts_used=0,
            expires_at=EXPIRES_AT,
            slot_frozen_at=None,
        )


def test_awaiting_confirmations_without_an_active_txid_cannot_be_built():
    """Paired with the one above: matched sets both fields in one transition.

    Without this guard the snapshot would quietly refuse the owner of the slot
    -- None never equals a txid -- so an effect the caller failed to persist
    would look like ordinary operation instead of a bug.
    """
    with pytest.raises(ValueError, match="active_txid"):
        InvoiceSnapshot(
            status=InvoiceStatus.AWAITING_CONFIRMATIONS,
            invoice_amount_cents=1,
            attempts_used=0,
            expires_at=EXPIRES_AT,
            slot_frozen_at=NOW,
            active_txid=None,
        )


def test_every_status_answers_every_event():
    """Totality across the whole surface: six statuses times four events."""
    from app.domain.events import ConfirmationsObserved, TxidVerdict
    from app.domain.statuses import Verdict

    events = [
        TxidAdmission(txid="0x1"),
        TxidVerdict(verdict=Verdict.MATCHED, txid="0x1"),
        ConfirmationsObserved(confirmations=1, raw_amount=1),
        TICK,
    ]
    for status in InvoiceStatus:
        for event in events:
            decision = decide(
                snapshot(status), event, datetime(2026, 8, 26, 12, 0, tzinfo=UTC), POLICY_6
            )
            assert decision.next_status in set(InvoiceStatus)
