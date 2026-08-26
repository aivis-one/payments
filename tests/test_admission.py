"""TxidAdmission: the pre-explorer guard, one test per cell of table 1.

The five refusing statuses get five separate tests rather than one
parametrised "everything else": a parametrised catch-all cannot show which
status nobody thought about.
"""

from __future__ import annotations

from datetime import timedelta

from app.domain.events import TxidAdmission
from app.domain.statuses import InvoiceStatus
from app.domain.transitions import decide
from tests.conftest import EXPIRES_AT, NOW, POLICY_6, snapshot

ADMISSION = TxidAdmission(txid="0xabc")


def test_created_within_ttl_and_budget_is_admitted():
    decision = decide(snapshot(InvoiceStatus.CREATED), ADMISSION, NOW, POLICY_6)

    assert decision.accepted
    assert decision.next_status is InvoiceStatus.CREATED
    assert decision.effects.attempts_used_delta == 0
    assert decision.attempt_record is None


def test_created_past_ttl_resolves_to_expired_and_refuses():
    """A refusal that carries a status change: both halves must be present."""
    decision = decide(snapshot(InvoiceStatus.CREATED), ADMISSION, EXPIRES_AT, POLICY_6)

    assert not decision.accepted
    assert decision.refused_by is InvoiceStatus.EXPIRED
    assert decision.next_status is InvoiceStatus.EXPIRED
    assert decision.effects.attempts_used_delta == 0


def test_created_with_exhausted_budget_resolves_to_attempts_exhausted():
    """Reachable only because the budget is configurable and can be lowered."""
    decision = decide(
        snapshot(InvoiceStatus.CREATED, attempts_used=4), ADMISSION, NOW, POLICY_6
    )

    assert decision.refused_by is InvoiceStatus.ATTEMPTS_EXHAUSTED
    assert decision.next_status is InvoiceStatus.ATTEMPTS_EXHAUSTED
    assert decision.effects.attempts_used_delta == 0


def test_created_at_exact_budget_ceiling_refuses():
    """>= not ==: attempts_used equal to the ceiling is already exhausted."""
    decision = decide(
        snapshot(InvoiceStatus.CREATED, attempts_used=3), ADMISSION, NOW, POLICY_6
    )

    assert decision.refused_by is InvoiceStatus.ATTEMPTS_EXHAUSTED


def test_expiry_wins_over_exhausted_budget():
    """Both conditions at once: expiry is terminal, so it takes precedence."""
    decision = decide(
        snapshot(InvoiceStatus.CREATED, attempts_used=9), ADMISSION, EXPIRES_AT, POLICY_6
    )

    assert decision.next_status is InvoiceStatus.EXPIRED
    assert decision.refused_by is InvoiceStatus.EXPIRED


def test_awaiting_confirmations_refuses_slot_occupied():
    decision = decide(snapshot(InvoiceStatus.AWAITING_CONFIRMATIONS), ADMISSION, NOW, POLICY_6)

    assert decision.refused_by is InvoiceStatus.AWAITING_CONFIRMATIONS
    assert decision.next_status is InvoiceStatus.AWAITING_CONFIRMATIONS


def test_awaiting_confirmations_past_window_refuses_as_stalled():
    frozen = NOW - timedelta(days=8)
    decision = decide(
        snapshot(InvoiceStatus.AWAITING_CONFIRMATIONS, slot_frozen_at=frozen),
        ADMISSION,
        NOW,
        POLICY_6,
    )

    assert decision.refused_by is InvoiceStatus.STALLED
    assert decision.next_status is InvoiceStatus.STALLED


def test_attempts_exhausted_within_ttl_refuses():
    decision = decide(snapshot(InvoiceStatus.ATTEMPTS_EXHAUSTED), ADMISSION, NOW, POLICY_6)

    assert decision.refused_by is InvoiceStatus.ATTEMPTS_EXHAUSTED
    assert decision.next_status is InvoiceStatus.ATTEMPTS_EXHAUSTED


def test_attempts_exhausted_past_ttl_refuses_as_expired():
    decision = decide(
        snapshot(InvoiceStatus.ATTEMPTS_EXHAUSTED), ADMISSION, EXPIRES_AT, POLICY_6
    )

    assert decision.refused_by is InvoiceStatus.EXPIRED
    assert decision.next_status is InvoiceStatus.EXPIRED


def test_confirmed_refuses_and_is_never_resolved_by_time():
    decision = decide(
        snapshot(InvoiceStatus.CONFIRMED), ADMISSION, EXPIRES_AT + timedelta(days=365), POLICY_6
    )

    assert decision.refused_by is InvoiceStatus.CONFIRMED
    assert decision.next_status is InvoiceStatus.CONFIRMED


def test_expired_refuses():
    decision = decide(snapshot(InvoiceStatus.EXPIRED), ADMISSION, NOW, POLICY_6)

    assert decision.refused_by is InvoiceStatus.EXPIRED


def test_stalled_refuses():
    decision = decide(snapshot(InvoiceStatus.STALLED), ADMISSION, NOW, POLICY_6)

    assert decision.refused_by is InvoiceStatus.STALLED


def test_no_admission_ever_spends_an_attempt_or_writes_a_row():
    """The guard runs before the explorer, so there is nothing to charge for."""
    for status in InvoiceStatus:
        decision = decide(snapshot(status), ADMISSION, NOW, POLICY_6)
        assert decision.effects.attempts_used_delta == 0
        assert decision.attempt_record is None


def test_admission_is_total_over_all_six_statuses():
    for status in InvoiceStatus:
        decide(snapshot(status), ADMISSION, NOW, POLICY_6)
