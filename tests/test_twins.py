"""Negative twins: repeat / emptiness / shortfall on the domain-level inputs.

The database half of the txid axis lives in test_partial_index.py, where the
uniqueness rule is actually enforced.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.domain.events import ConfirmationsObserved, TxidAdmission, TxidVerdict
from app.domain.statuses import InvoiceStatus, Verdict
from app.domain.transitions import decide
from tests.conftest import NOW, POLICY_6, snapshot

# --------------------------------------------------------------------------
# txid
# --------------------------------------------------------------------------


def test_txid_repeat_within_one_invoice_is_the_callers_problem_not_a_branch():
    """Two identical verdicts produce two identical decisions.

    The function is a function: it has no memory of the previous TXID. What
    saves a double-clicking user from losing an attempt is H3 re-reading the
    winning row after IntegrityError and returning the winner's result when the
    row belongs to the same invoice. already_used therefore means one thing
    only: the TXID is held by a *different* invoice.
    """
    base = snapshot(InvoiceStatus.CREATED, attempts_used=1)
    event = TxidVerdict(verdict=Verdict.NOT_FOUND, txid="0xsame")

    first = decide(base, event, NOW, POLICY_6)
    second = decide(base, event, NOW, POLICY_6)

    assert first == second


def test_empty_txid_costs_nothing():
    """An empty string is a format rejection, and formats never reach the explorer."""
    decision = decide(
        snapshot(InvoiceStatus.CREATED, attempts_used=2),
        TxidVerdict(verdict=Verdict.INVALID_FORMAT, txid=""),
        NOW,
        POLICY_6,
    )

    assert decision.next_status is InvoiceStatus.CREATED
    assert decision.effects.attempts_used_delta == 0
    assert decision.attempt_record is None


def test_empty_txid_at_admission_does_not_crash_the_guard():
    """The guard runs before format validation, so it must tolerate anything."""
    decision = decide(snapshot(InvoiceStatus.CREATED), TxidAdmission(txid=""), NOW, POLICY_6)

    assert decision.accepted


def test_truncated_txid_takes_the_same_path_as_any_other_format_rejection():
    """63 hex characters instead of 64: still a format rejection, still free."""
    truncated = "a" * 63
    decision = decide(
        snapshot(InvoiceStatus.CREATED),
        TxidVerdict(verdict=Verdict.INVALID_FORMAT, txid=truncated),
        NOW,
        POLICY_6,
    )

    assert decision.effects.attempts_used_delta == 0


def test_matched_txid_is_carried_into_the_effects_verbatim():
    """Whatever string arrives is what occupies the slot; no normalisation here."""
    weird = "  0xAbC  "
    decision = decide(
        snapshot(InvoiceStatus.CREATED),
        TxidVerdict(verdict=Verdict.MATCHED, txid=weird),
        NOW,
        POLICY_6,
    )

    assert decision.effects.active_txid == weird


# --------------------------------------------------------------------------
# invoice_amount_cents
# --------------------------------------------------------------------------


def test_two_invoices_with_identical_amounts_are_independent():
    """The amount is not a key; repeat carries no meaning."""
    one = snapshot(InvoiceStatus.CREATED, invoice_amount_cents=10_000)
    two = snapshot(InvoiceStatus.AWAITING_CONFIRMATIONS, invoice_amount_cents=10_000)

    admitted = decide(one, TxidAdmission(txid="0x1"), NOW, POLICY_6)
    refused = decide(two, TxidAdmission(txid="0x1"), NOW, POLICY_6)

    assert admitted.accepted
    assert not refused.accepted


def test_zero_invoice_amount_confirms_and_is_never_underpaid():
    """No lower bound here by design; the bound belongs to the request schema.

    There is no CHECK on the column either, so zero is genuinely reachable and
    this test is honest. If a CHECK is ever added, this test starts building an
    unreachable value by hand and must move to the H3 schema instead.
    """
    decision = decide(
        snapshot(InvoiceStatus.AWAITING_CONFIRMATIONS, invoice_amount_cents=0),
        ConfirmationsObserved(confirmations=20, raw_amount=0),
        NOW,
        POLICY_6,
    )

    assert decision.next_status is InvoiceStatus.CONFIRMED
    assert decision.effects.credited_amount_cents == 0
    assert decision.effects.underpaid is False


@pytest.mark.parametrize(
    ("raw", "expected_cents", "underpaid"),
    [
        (99_999_999, 9_999, True),  # one cent short
        (100_000_000, 10_000, False),  # exact
        (100_010_000, 10_001, False),  # one cent over
    ],
)
def test_shortfall_boundary_is_exact_to_the_cent(raw: int, expected_cents: int, underpaid: bool):
    decision = decide(
        snapshot(InvoiceStatus.AWAITING_CONFIRMATIONS, invoice_amount_cents=10_000),
        ConfirmationsObserved(confirmations=20, raw_amount=raw),
        NOW,
        POLICY_6,
    )

    assert decision.effects.credited_amount_cents == expected_cents
    assert decision.effects.underpaid is underpaid


def test_negative_attempts_used_cannot_be_built():
    with pytest.raises(ValueError, match="attempts_used"):
        snapshot(InvoiceStatus.CREATED, attempts_used=-1)


# --------------------------------------------------------------------------
# credited_amount_cents
# --------------------------------------------------------------------------


def test_no_credit_is_produced_below_the_confirmation_threshold():
    """The invoice carries no amount at all until it enters confirmed."""
    decision = decide(
        snapshot(InvoiceStatus.AWAITING_CONFIRMATIONS, slot_frozen_at=NOW - timedelta(minutes=1)),
        ConfirmationsObserved(confirmations=1, raw_amount=100_000_000),
        NOW,
        POLICY_6,
    )

    assert decision.effects.credited_amount_cents is None
    assert decision.effects.underpaid is None


def test_stalling_produces_no_credit_even_with_an_amount_in_hand():
    decision = decide(
        snapshot(InvoiceStatus.AWAITING_CONFIRMATIONS, slot_frozen_at=NOW - timedelta(days=9)),
        ConfirmationsObserved(confirmations=1, raw_amount=100_000_000),
        NOW,
        POLICY_6,
    )

    assert decision.next_status is InvoiceStatus.STALLED
    assert decision.effects.credited_amount_cents is None
