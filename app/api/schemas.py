"""Request and response bodies (TOR section 8).

``extra="forbid"`` on the request models on purpose: a field the service does
not know about is far more often a typo or a version skew than a deliberate
extension, and silently dropping it lets the sender believe it took effect.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CreateInvoiceRequest(BaseModel):
    """Body of ``POST /api/v1/invoices``."""

    model_config = ConfigDict(extra="forbid")

    product_ref: str = Field(min_length=1, max_length=255)

    #: Validated against the served networks in the route, not here. A plain
    #: ``str`` because TOR section 12 keeps the network list open: an enum
    #: would turn adding a fourth network into a schema change, and would also
    #: answer an unserved network with a schema error, which is precisely the
    #: confusion 400 versus 422 exists to avoid.
    network: str = Field(min_length=1)

    #: At least one cent. There is no CHECK on the column and none is added:
    #: the domain genuinely accepts zero, and the transition tests that feed it
    #: zero stay honest because the floor lives at this boundary rather than in
    #: the database.
    invoice_amount_cents: int = Field(ge=1)


class SubmitTxidRequest(BaseModel):
    """Body of ``POST /api/v1/invoices/{id}/txid``.

    ``txid`` carries no ``min_length`` deliberately. An empty or malformed hash
    is not a schema violation -- TOR section 8 lists ``invalid_format`` among
    the result codes of a successful 200 response, and answering 422 instead
    would move a documented outcome into a different status class. A *missing*
    ``txid`` field is a schema violation and does get 422; the two cases are
    different and are tested separately.
    """

    model_config = ConfigDict(extra="forbid")

    txid: str


class InvoiceCreated(BaseModel):
    """Response of ``POST /api/v1/invoices``."""

    id: uuid.UUID
    network: str

    #: Snapshot taken at creation, not a live read of config. Verification
    #: compares against this value for the whole life of the invoice, so
    #: rotating the configured address cannot break an invoice already issued.
    address: str

    invoice_amount_cents: int
    status: str
    expires_at: datetime


class TxidResult(BaseModel):
    """Response of a submission that was accepted for processing.

    ``result_code`` is the explorer verdict, which is a wider vocabulary than
    the ``result_code`` column of ``invoice_txid_attempts``: ``api_error`` and
    ``invalid_format`` are answers the caller gets but never rows in the
    database, because neither costs an attempt. Two vocabularies with one name
    is a trap in TOR section 4 versus section 8; the wide one belongs here.
    """

    status: str
    result_code: str
    attempts_used: int
    attempts_remaining: int


class InvoiceView(BaseModel):
    """Response of ``GET /api/v1/invoices/{id}``.

    ``status`` is resolved against the clock before being returned, so it can
    be ahead of the stored row -- an invoice whose TTL passed reads ``expired``
    here while its row still says ``created``. Nothing is written to make them
    agree; the sweeper does that and emits the event.

    ``attempts_remaining`` is derived rather than stored, and is included even
    though TOR section 4 does not list it. Without it a client that reloads a
    page has ``attempts_used`` and no way to subtract from it --
    ``MAX_TXID_ATTEMPTS`` lives in this service's config and is not exposed --
    so it would hard-code three, putting a rule of this service inside the
    product. The submission response already returns the field; leaving it out
    of the read route would be an asymmetry that serves nobody.

    ``slot_frozen_at`` is not exposed: TOR section 8 marks it internal.
    """

    id: uuid.UUID
    product_ref: str
    network: str
    address: str
    invoice_amount_cents: int
    status: str
    credited_amount_cents: int | None
    underpaid: bool | None
    active_txid: str | None
    attempts_used: int
    attempts_remaining: int
    expires_at: datetime
    created_at: datetime
