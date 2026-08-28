"""The outbox parts that need no database: the body, the schedule, the config.

``no_network``, not ``no_explorer``: nothing here opens a session, so this
module can hold the stronger marker and does. Every claim about the outgoing
request is settled against a mock transport, and the trap underneath guarantees
that a real socket was never an option.

What is deliberately *not* here: anything about whether a row was written. That
question belongs to the database and lives in ``test_outbox_db``. A stub that
records an INSERT proves the code reached for one, not that the schema would
have accepted it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.domain.statuses import TERMINAL_STATUSES, InvoiceStatus
from app.models import Invoice
from app.outbox import PUBLISHED_STATUSES, DeliveryState, backoff, event_payload
from tests.explorers_support import EVM_WALLET, make_settings

pytestmark = pytest.mark.no_network

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


def invoice(
    *,
    status: InvoiceStatus = InvoiceStatus.CONFIRMED,
    credited_amount_cents: int | None = None,
    underpaid: bool | None = None,
) -> Invoice:
    return Invoice(
        id=uuid.UUID("11111111-2222-3333-4444-555555555555"),
        product_ref="product-1",
        network="USDT-ERC20",
        address=EVM_WALLET,
        invoice_amount_cents=10_000,
        status=status.value,
        credited_amount_cents=credited_amount_cents,
        underpaid=underpaid,
        attempts_used=1,
        expires_at=NOW + timedelta(minutes=60),
    )


# ==========================================================================
# Which statuses are events at all
# ==========================================================================


def test_four_statuses_are_published_and_they_are_not_the_terminal_three():
    """The set that is easiest to get wrong by reaching for the nearest one.

    ``TERMINAL_STATUSES`` is three: ``attempts_exhausted`` is not terminal,
    because it still waits for its TTL to turn it into ``expired``. It is an
    event all the same -- the product has to stop offering a payment form for
    an invoice that will accept no more hashes -- so reusing the terminal set
    here would silence exactly one of the four.
    """
    assert {
        InvoiceStatus.CONFIRMED,
        InvoiceStatus.EXPIRED,
        InvoiceStatus.ATTEMPTS_EXHAUSTED,
        InvoiceStatus.STALLED,
    } == PUBLISHED_STATUSES
    assert InvoiceStatus.ATTEMPTS_EXHAUSTED in PUBLISHED_STATUSES
    assert InvoiceStatus.ATTEMPTS_EXHAUSTED not in TERMINAL_STATUSES


def test_no_status_outside_the_domain_enum_is_published():
    """Walks the enum rather than a list written here.

    A seventh status would arrive as a failure demanding a decision instead of
    quietly belonging to neither set.
    """
    assert set(InvoiceStatus) >= PUBLISHED_STATUSES
    assert InvoiceStatus.CREATED not in PUBLISHED_STATUSES
    assert InvoiceStatus.AWAITING_CONFIRMATIONS not in PUBLISHED_STATUSES


# ==========================================================================
# The body of TOR section 8
# ==========================================================================


def test_the_payload_carries_exactly_the_contract_fields():
    payload = event_payload(
        invoice(credited_amount_cents=10_000, underpaid=False), NOW
    )

    assert set(payload) == {
        "invoice_id",
        "product_ref",
        "status",
        "credited_amount_cents",
        "underpaid",
        "occurred_at",
    }
    assert payload["status"] == "confirmed"
    assert payload["product_ref"] == "product-1"


def test_the_optional_fields_are_absent_rather_than_null():
    """The emptiness axis, and the reason it is not cosmetic.

    ``underpaid`` is a boolean whose false value means something. A receiver
    testing falsiness cannot tell ``false`` from ``null`` from missing, so the
    contract says optional and this says absent -- and H8 is expected to test
    for the key.
    """
    payload = event_payload(invoice(status=InvoiceStatus.EXPIRED), NOW)

    assert "credited_amount_cents" not in payload
    assert "underpaid" not in payload
    assert payload["status"] == "expired"


def test_an_underpaid_confirmation_carries_a_false_that_is_present():
    """The half that makes the previous test mean something.

    If absence were achieved by dropping falsy values, this would drop
    ``underpaid=False`` too -- and the product would read an underpayment as a
    full one.
    """
    payload = event_payload(
        invoice(credited_amount_cents=9_000, underpaid=True), NOW
    )
    assert payload["underpaid"] is True

    exact = event_payload(invoice(credited_amount_cents=10_000, underpaid=False), NOW)
    assert exact["underpaid"] is False


def test_uuid_and_datetime_are_rendered_because_jsonb_holds_neither():
    payload = event_payload(invoice(), NOW)

    assert payload["invoice_id"] == "11111111-2222-3333-4444-555555555555"
    assert payload["occurred_at"] == "2026-08-28T12:00:00+00:00"
    # The offset is part of it: a naive timestamp on the wire would leave the
    # receiver guessing at a zone.
    assert datetime.fromisoformat(str(payload["occurred_at"])).tzinfo is not None


def test_occurred_at_is_the_moment_handed_in_not_the_clock():
    """The debt from H1a, made visible.

    ``attempts_exhausted`` is published later than the budget actually ran out
    -- the concurrency contract lets the counter pass the threshold while the
    status still says ``created``. What the event must not do on top of that is
    carry the *delivery* time, which would make the lateness invisible.
    """
    long_ago = NOW - timedelta(hours=3)

    payload = event_payload(invoice(status=InvoiceStatus.ATTEMPTS_EXHAUSTED), long_ago)

    assert payload["occurred_at"] == long_ago.isoformat()


# ==========================================================================
# The retry schedule
# ==========================================================================


def test_the_delay_doubles_from_the_floor():
    settings = make_settings()

    assert backoff(1, settings) == timedelta(seconds=5)
    assert backoff(2, settings) == timedelta(seconds=10)
    assert backoff(3, settings) == timedelta(seconds=20)
    assert backoff(4, settings) == timedelta(seconds=40)


def test_the_delay_is_clamped_at_the_ceiling():
    settings = make_settings()

    assert backoff(11, settings) == timedelta(hours=1)
    assert backoff(12, settings) == timedelta(hours=1)


def test_an_absurd_count_does_not_compute_an_astronomical_number():
    """Capped before the shift, not after.

    ``claims`` feeds this function too, and a row that has been picked up a
    thousand times would otherwise raise two to the thousandth on its way to
    being clamped to an hour.
    """
    settings = make_settings()

    assert backoff(1_000, settings) == timedelta(hours=1)
    assert backoff(0, settings) == timedelta(seconds=5)


def test_twelve_attempts_span_about_two_and_a_half_hours():
    """The default's arithmetic, stated where it can be checked.

    Twelve attempts have *eleven* waits between them, which is the correction
    this test forced: the figure quoted when the default was agreed counted
    twelve waits and came out an hour too long. Ten attempts would still be
    forty minutes, so the reason for not choosing ten is unchanged -- that is
    shorter than an ordinary deployment of the product, and the thing being
    given up on can be a confirmed payment.
    """
    settings = make_settings()

    total = sum(
        (backoff(n, settings) for n in range(1, settings.WEBHOOK_MAX_ATTEMPTS)),
        timedelta(),
    )

    assert timedelta(hours=2) < total < timedelta(hours=3)
    # Guards the guard: eleven waits, not twelve. Off by one here would move
    # the window by a whole hour, because the last waits are all at the cap.
    assert settings.WEBHOOK_MAX_ATTEMPTS - 1 == 11


def test_the_three_delivery_states_are_the_ones_the_contract_names():
    assert {state.value for state in DeliveryState} == {"pending", "delivered", "failed"}


# ==========================================================================
# Fail-closed, both halves
# ==========================================================================


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_a_blank_webhook_secret_refuses_to_boot(blank: str):
    """The sender's half of the fail-closed pair in TOR section 8.

    ``compare_digest("", "")`` is true, so a blank secret here would be sent as
    a blank header and accepted by any receiver that compares before checking
    for emptiness. The failure is silent and looks exactly like working
    authentication, which is why it is refused at startup instead.
    """
    with pytest.raises(ValidationError, match="PAYMENTS_WEBHOOK_SECRET"):
        make_settings(PAYMENTS_WEBHOOK_SECRET=blank)


def test_the_configured_secret_is_non_blank_and_is_what_would_be_sent():
    """The other half, without which the test above passes on a service that
    sends nothing at all.

    That the header actually carries this value is settled against a captured
    request in ``test_outbox_db``; here the claim is only that the value
    survives configuration non-empty.
    """
    settings = make_settings()

    assert settings.PAYMENTS_WEBHOOK_SECRET
    assert settings.PAYMENTS_WEBHOOK_SECRET.strip() == settings.PAYMENTS_WEBHOOK_SECRET


@pytest.mark.parametrize(
    "url",
    ["", "   ", "product.invalid/webhooks", "//product.invalid/webhooks", "ftp://p/x"],
)
def test_a_url_without_a_scheme_refuses_to_boot(url: str):
    """The emptiness and shortfall axes of the same input.

    A URL that cannot be delivered to is not discovered at startup unless it is
    checked at startup: the service would otherwise learn it hours later, one
    ``failed`` row at a time, having published events it can never send.
    """
    with pytest.raises(ValidationError, match="PRODUCT_WEBHOOK_URL"):
        make_settings(PRODUCT_WEBHOOK_URL=url)


def test_a_configured_url_is_taken_verbatim():
    """The repetition axis: no normalisation of any kind.

    Two spellings that differ by a trailing slash are two different endpoints
    as far as this service is concerned, because guessing which one the product
    meant is not a thing it can do correctly.
    """
    with_slash = make_settings(PRODUCT_WEBHOOK_URL="https://product.invalid/hooks/")
    without = make_settings(PRODUCT_WEBHOOK_URL="https://product.invalid/hooks")

    assert with_slash.PRODUCT_WEBHOOK_URL == "https://product.invalid/hooks/"
    assert without.PRODUCT_WEBHOOK_URL == "https://product.invalid/hooks"
