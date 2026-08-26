"""TxidVerdict: what the explorer's answer does to the invoice (table 2)."""

from __future__ import annotations

import pytest

from app.domain.events import TxidVerdict
from app.domain.statuses import AttemptResultCode, InvoiceStatus, Verdict
from app.domain.transitions import decide
from tests.conftest import EXPIRES_AT, NOW, POLICY_6, REFUSING_STATUSES, snapshot

TXID = "0xdeadbeef"

#: Verdicts that spend an attempt and write a row.
SPENDING = [
    Verdict.NOT_FOUND,
    Verdict.WRONG_ADDRESS,
    Verdict.WRONG_NETWORK,
    Verdict.ALREADY_USED,
]

#: Verdicts that cost nothing and write nothing.
FREE = [Verdict.API_ERROR, Verdict.INVALID_FORMAT]


def test_matched_takes_the_slot():
    decision = decide(
        snapshot(InvoiceStatus.CREATED),
        TxidVerdict(verdict=Verdict.MATCHED, txid=TXID),
        NOW,
        POLICY_6,
    )

    assert decision.next_status is InvoiceStatus.AWAITING_CONFIRMATIONS
    assert decision.effects.slot_frozen_at == NOW
    assert decision.effects.active_txid == TXID
    assert decision.effects.attempts_used_delta == 1
    assert decision.attempt_record is AttemptResultCode.MATCHED


def test_matched_on_the_last_allowed_attempt_still_takes_the_slot():
    """The budget is not re-checked after a matched increment.

    Comparing >= MAX after every increment would drown a valid payment that
    arrived on the last attempt in attempts_exhausted.
    """
    decision = decide(
        snapshot(InvoiceStatus.CREATED, attempts_used=2),
        TxidVerdict(verdict=Verdict.MATCHED, txid=TXID),
        NOW,
        POLICY_6,
    )

    assert decision.next_status is InvoiceStatus.AWAITING_CONFIRMATIONS
    assert decision.effects.attempts_used_delta == 1


@pytest.mark.parametrize("verdict", SPENDING)
def test_rejection_below_ceiling_returns_to_created(verdict: Verdict):
    decision = decide(
        snapshot(InvoiceStatus.CREATED, attempts_used=0),
        TxidVerdict(verdict=verdict, txid=TXID),
        NOW,
        POLICY_6,
    )

    assert decision.next_status is InvoiceStatus.CREATED
    assert decision.effects.attempts_used_delta == 1
    assert decision.attempt_record == AttemptResultCode(verdict.value)


@pytest.mark.parametrize("verdict", SPENDING)
def test_rejection_reaching_ceiling_exhausts(verdict: Verdict):
    """Increment first, then compare: the third rejection is the last one."""
    decision = decide(
        snapshot(InvoiceStatus.CREATED, attempts_used=2),
        TxidVerdict(verdict=verdict, txid=TXID),
        NOW,
        POLICY_6,
    )

    assert decision.next_status is InvoiceStatus.ATTEMPTS_EXHAUSTED
    assert decision.effects.attempts_used_delta == 1


def test_rejection_above_ceiling_still_exhausts():
    """>= keeps the function total after MAX_TXID_ATTEMPTS is lowered.

    With == the invoice at attempts_used=5 against a ceiling of 3 would match
    neither branch.
    """
    decision = decide(
        snapshot(InvoiceStatus.CREATED, attempts_used=5),
        TxidVerdict(verdict=Verdict.NOT_FOUND, txid=TXID),
        NOW,
        POLICY_6,
    )

    assert decision.next_status is InvoiceStatus.ATTEMPTS_EXHAUSTED


@pytest.mark.parametrize("verdict", FREE)
def test_infrastructure_and_format_cost_nothing(verdict: Verdict):
    """api_error and invalid_format are both on the service's side of the line."""
    decision = decide(
        snapshot(InvoiceStatus.CREATED, attempts_used=2),
        TxidVerdict(verdict=verdict, txid=TXID),
        NOW,
        POLICY_6,
    )

    assert decision.next_status is InvoiceStatus.CREATED
    assert decision.effects.attempts_used_delta == 0
    assert decision.attempt_record is None


def test_not_found_is_a_real_attempt_unlike_api_error():
    """The distinction required by the test band, in the half H1 can observe.

    NOT_FOUND here always means the internal retry window is already exhausted:
    the adapter hands down a final verdict only. The other half -- that inside
    the window nothing is spent -- is not observable through this function and
    is an obligation of H2/H3.
    """
    base = snapshot(InvoiceStatus.CREATED, attempts_used=1)

    not_found = decide(base, TxidVerdict(verdict=Verdict.NOT_FOUND, txid=TXID), NOW, POLICY_6)
    api_error = decide(base, TxidVerdict(verdict=Verdict.API_ERROR, txid=TXID), NOW, POLICY_6)

    assert not_found.effects.attempts_used_delta == 1
    assert api_error.effects.attempts_used_delta == 0


def test_verdict_does_not_re_resolve_time():
    """Up to ~7s of internal retry may pass; a payment found on-chain wins."""
    decision = decide(
        snapshot(InvoiceStatus.CREATED),
        TxidVerdict(verdict=Verdict.MATCHED, txid=TXID),
        EXPIRES_AT,
        POLICY_6,
    )

    assert decision.next_status is InvoiceStatus.AWAITING_CONFIRMATIONS
    assert decision.accepted


@pytest.mark.parametrize("status", REFUSING_STATUSES)
def test_verdict_for_a_non_created_invoice_is_refused_not_raised(status: InvoiceStatus):
    """A race H3 closes by re-reading; the function answers instead of raising."""
    decision = decide(
        snapshot(status), TxidVerdict(verdict=Verdict.MATCHED, txid=TXID), NOW, POLICY_6
    )

    assert decision.refused_by is status
    assert decision.next_status is status
    assert decision.effects.attempts_used_delta == 0
    assert decision.attempt_record is None


def test_verdict_is_total_over_every_status_and_verdict():
    for status in InvoiceStatus:
        for verdict in Verdict:
            decide(snapshot(status), TxidVerdict(verdict=verdict, txid=TXID), NOW, POLICY_6)


def test_only_five_verdicts_ever_become_a_row():
    """api_error and invalid_format have no result_code to be written as."""
    recorded = {
        decide(
            snapshot(InvoiceStatus.CREATED), TxidVerdict(verdict=v, txid=TXID), NOW, POLICY_6
        ).attempt_record
        for v in Verdict
    }

    assert recorded == {None, *AttemptResultCode}
    assert len(AttemptResultCode) == 5
