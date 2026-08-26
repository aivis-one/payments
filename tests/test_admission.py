"""TxidAdmission: the pre-explorer guard, one test per cell of table 1.

The five refusing statuses get five separate tests rather than one
parametrised "everything else": a parametrised catch-all cannot show which
status nobody thought about.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.domain.events import ConfirmationsObserved, TxidAdmission
from app.domain.statuses import InvoiceStatus
from app.domain.transitions import NO_EFFECTS, Decision, decide
from tests.conftest import ACTIVE_TXID, EXPIRES_AT, NOW, POLICY_6, snapshot

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


# --------------------------------------------------------------------------
# Idempotent replay: the same TXID submitted again while it holds the slot.
#
# The pair matters more than either half. "The owner's own TXID is not refused"
# would pass just as well against a guard broken open for everything, so it is
# always read next to "a foreign TXID is still refused".
# --------------------------------------------------------------------------


def test_own_txid_while_it_holds_the_slot_is_not_refused():
    """Refusing would be answered as slot_occupied, which asserts "another TXID"."""
    decision = decide(
        snapshot(InvoiceStatus.AWAITING_CONFIRMATIONS),
        TxidAdmission(txid=ACTIVE_TXID),
        NOW,
        POLICY_6,
    )

    assert decision.accepted
    assert decision.refused_by is None
    assert decision.idempotent_replay is True
    assert decision.next_status is InvoiceStatus.AWAITING_CONFIRMATIONS


def test_foreign_txid_against_an_occupied_slot_is_still_refused():
    """The other half of the pair: the guard did not open for everything."""
    decision = decide(
        snapshot(InvoiceStatus.AWAITING_CONFIRMATIONS),
        TxidAdmission(txid="0xsomeoneelse"),
        NOW,
        POLICY_6,
    )

    assert not decision.accepted
    assert decision.refused_by is InvoiceStatus.AWAITING_CONFIRMATIONS
    assert decision.idempotent_replay is False


def test_replay_costs_nothing_and_persists_nothing():
    decision = decide(
        snapshot(InvoiceStatus.AWAITING_CONFIRMATIONS, attempts_used=1),
        TxidAdmission(txid=ACTIVE_TXID),
        NOW,
        POLICY_6,
    )

    assert decision.effects == NO_EFFECTS
    assert decision.attempt_record is None


def test_own_txid_after_the_observation_window_is_refused_as_stalled():
    """The time resolve runs first, and stalled is never reopened.

    "But it is my own TXID" is not one of the conditions under which the
    service reopens a stalled invoice, because there are none.
    """
    decision = decide(
        snapshot(InvoiceStatus.AWAITING_CONFIRMATIONS, slot_frozen_at=NOW - timedelta(days=8)),
        TxidAdmission(txid=ACTIVE_TXID),
        NOW,
        POLICY_6,
    )

    assert decision.refused_by is InvoiceStatus.STALLED
    assert decision.idempotent_replay is False


def test_own_txid_on_a_confirmed_invoice_is_still_refused():
    """Only slot_occupied would have lied; invoice_already_confirmed does not.

    The invoice really is confirmed, so refusing states a true fact about it
    and no replay cell is needed for this status.
    """
    decision = decide(
        snapshot(InvoiceStatus.CONFIRMED, active_txid=ACTIVE_TXID),
        TxidAdmission(txid=ACTIVE_TXID),
        NOW,
        POLICY_6,
    )

    assert decision.refused_by is InvoiceStatus.CONFIRMED
    assert decision.idempotent_replay is False


def test_admission_stays_total_with_the_replay_cell_in_place():
    for status in InvoiceStatus:
        for txid in (ACTIVE_TXID, "0xforeign", ""):
            decide(snapshot(status), TxidAdmission(txid=txid), NOW, POLICY_6)


def test_waiting_for_confirmations_is_not_reported_as_a_replay():
    """The shape the flag exists for: same status, same acceptance, other meaning.

    An observation below the threshold returns accepted with next_status
    awaiting_confirmations too. If replay were derived from that combination
    instead of carried explicitly, this decision would be misread as a replay.
    """
    decision = decide(
        snapshot(InvoiceStatus.AWAITING_CONFIRMATIONS),
        ConfirmationsObserved(confirmations=1, raw_amount=100_000_000),
        NOW,
        POLICY_6,
    )

    assert decision.accepted
    assert decision.next_status is InvoiceStatus.AWAITING_CONFIRMATIONS
    assert decision.idempotent_replay is False


def test_a_refusal_cannot_also_be_a_replay():
    """Locked in the constructor: two fields able to disagree, kept from doing so."""
    with pytest.raises(ValueError, match="idempotent replay"):
        Decision(
            next_status=InvoiceStatus.AWAITING_CONFIRMATIONS,
            refused_by=InvoiceStatus.AWAITING_CONFIRMATIONS,
            idempotent_replay=True,
        )
