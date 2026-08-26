"""Domain vocabularies: invoice statuses, explorer verdicts, attempt records.

Three separate enums on purpose. They look similar and are not the same thing:
a verdict is what the explorer layer concluded, an attempt record is what gets
persisted, and only five of the seven verdicts ever become a row.
"""

from __future__ import annotations

from enum import StrEnum


class InvoiceStatus(StrEnum):
    """The six persistent statuses of an invoice (TOR sections 4 and 5).

    ``submitted`` is absent deliberately. TOR section 4 states it is not
    persisted: it is the in-flight interval of a single HTTP request, between
    format validation and the explorer's answer. Adding it here would create a
    seventh member that can never be read back from the database -- an
    unreachable state that tests could only build by hand. The interval itself
    is modelled as two events (``TxidAdmission`` then ``TxidVerdict``), not as
    a status.
    """

    CREATED = "created"
    AWAITING_CONFIRMATIONS = "awaiting_confirmations"
    CONFIRMED = "confirmed"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    EXPIRED = "expired"
    STALLED = "stalled"


#: Statuses the service never leaves on its own.
#:
#: ``attempts_exhausted`` is NOT here: it accepts no new TXID but still waits
#: for its TTL to turn it into ``expired`` (TOR section 5).
#:
#: On the terminality of ``confirmed`` specifically -- see the KNOWN CEILING
#: marker in ``app/domain/transitions.py`` (reorg after ``confirmed``). Full
#: text lives there and only there.
TERMINAL_STATUSES: frozenset[InvoiceStatus] = frozenset(
    {InvoiceStatus.CONFIRMED, InvoiceStatus.EXPIRED, InvoiceStatus.STALLED}
)


class Verdict(StrEnum):
    """What the explorer layer (H2) concluded about one submitted TXID.

    Seven members. There is no member for "not found, but the internal retry
    window is not exhausted yet": the adapter contract is that the retry loop
    sits *above* the adapter and the adapter hands down a final verdict only.
    So ``NOT_FOUND`` here always means a real, spent user attempt.

    The consequence is that "inside the retry window the attempt is not spent"
    is not observable through this function at all -- it is an obligation of
    H2/H3. Said out loud in the delivery report rather than papered over with a
    branch that would never fire here.
    """

    MATCHED = "matched"
    NOT_FOUND = "not_found"
    WRONG_ADDRESS = "wrong_address"
    WRONG_NETWORK = "wrong_network"
    ALREADY_USED = "already_used"
    API_ERROR = "api_error"
    INVALID_FORMAT = "invalid_format"


class AttemptResultCode(StrEnum):
    """Values that can actually appear in ``invoice_txid_attempts.result_code``.

    Five, not seven. ``api_error`` never becomes a row at all (TOR section 4,
    "Важно"), and ``invalid_format`` is rejected by regex before the explorer
    is called, so it has no row either (TOR section 7). Both are therefore
    unreachable in this column and are not listed as members.

    ``already_used`` means one thing only: the TXID is already held by a
    *different* invoice. A second concurrent request carrying the same TXID for
    the *same* invoice is an idempotent replay -- H3 re-reads the winning row
    after ``IntegrityError`` and returns the winner's result, so a double click
    does not cost the user an attempt.
    """

    MATCHED = "matched"
    NOT_FOUND = "not_found"
    WRONG_ADDRESS = "wrong_address"
    WRONG_NETWORK = "wrong_network"
    ALREADY_USED = "already_used"
