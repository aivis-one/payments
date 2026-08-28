"""Route behaviour that does not depend on a database.

Split from ``test_routes_db.py`` on purpose. Everything here is decided before
any SQL runs -- credentials, body shape, whether we serve the network, whether
the TXID is even shaped like one -- so it can be checked on any machine. The
moment a test is really about persistence, savepoints or the unique index, it
belongs in the other file, where it needs Postgres and is proved on the server.

The stub session below is a test double for SQLAlchemy's API, not an
abstraction introduced into the application: the routes take an
``AsyncSession`` exactly as they do in production. It answers ``get`` and
refuses everything else, so a test that claims not to touch the database fails
loudly if it does.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from starlette.requests import Request

from app.api.deps import explorer_client, require_service_token, settings_dependency
from app.api.errors import REFUSAL_CODE, ApiError
from app.db import get_session
from app.domain.statuses import InvoiceStatus
from app.main import app
from app.models import Invoice
from tests.explorers_support import (
    EVM_TXID,
    EVM_WALLET,
    TRON_WALLET,
    RecordingTransport,
    json_response,
    make_settings,
)

pytestmark = pytest.mark.no_network

TOKEN = "test-token-not-real"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class StubSession:
    """Answers ``get`` from a dict; screams if anything else is attempted."""

    def __init__(self, rows: dict[uuid.UUID, Invoice] | None = None) -> None:
        self.rows = rows or {}
        self.commits = 0
        self.rollbacks = 0
        self.added: list[object] = []
        self.executed: list[object] = []

    async def get(self, _model: object, key: uuid.UUID) -> Invoice | None:
        return self.rows.get(key)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1
        # SQLAlchemy applies python-side column defaults at flush, and
        # ``Invoice.id`` has one. Without this the route looks broken in a way
        # it is not: the id is populated in production and only missing here.
        # Server-side defaults are NOT emulated -- nothing in a creation
        # response depends on one.
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid.uuid4()

    async def refresh(self, _obj: object) -> None:
        return None

    async def rollback(self) -> None:
        # Counted rather than ignored: the route releases the connection
        # before the explorer call, and "it was released" is only half the
        # claim. The other half -- that releasing it gives the property --
        # needs a real transaction and lives in test_routes_db.
        self.rollbacks += 1

    async def execute(self, statement: object, *_args: object, **_kw: object) -> None:
        # Recorded, not refused: publishing an event is a Core INSERT, and a
        # route that enters a published status now issues one. What the row
        # contains is settled against a real database in test_outbox_db; here
        # the only claim is that the route reached for it at all.
        self.executed.append(statement)

    async def scalar(self, *_args: object, **_kw: object) -> Any:
        raise AssertionError("a query was executed by a test that claims not to need one")

    def begin_nested(self) -> Any:
        raise AssertionError("a savepoint was opened by a test that claims not to need one")


def invoice(
    *,
    status: InvoiceStatus = InvoiceStatus.CREATED,
    attempts_used: int = 0,
    ttl_minutes: int = 60,
    network: str = "USDT-ERC20",
    address: str = EVM_WALLET,
    active_txid: str | None = None,
    slot_frozen_at: datetime | None = None,
) -> Invoice:
    now = datetime.now(UTC)
    return Invoice(
        id=uuid.uuid4(),
        product_ref="product-1",
        network=network,
        address=address,
        invoice_amount_cents=10_000,
        status=status.value,
        attempts_used=attempts_used,
        active_txid=active_txid,
        slot_frozen_at=slot_frozen_at,
        expires_at=now + timedelta(minutes=ttl_minutes),
        created_at=now,
    )


def client_for(
    session: StubSession, transport: RecordingTransport | None = None
) -> httpx.AsyncClient:
    """An ASGI client with every external dependency replaced.

    ``ASGITransport`` speaks to the application in-process and opens no socket,
    so it is untouched by the ``no_network`` trap this module carries -- which
    still guards the explorer client underneath.
    """
    explorer = httpx.AsyncClient(transport=transport or RecordingTransport())
    # A zero-argument lambda, not ``make_settings`` itself: FastAPI inspects a
    # dependency's signature, and ``**overrides`` there would be read as request
    # parameters rather than as a factory's keyword arguments.
    settings = make_settings()
    app.dependency_overrides[settings_dependency] = lambda: settings
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[explorer_client] = lambda: explorer
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


# ==========================================================================
# The refusal table
# ==========================================================================


def test_the_refusal_table_is_total_over_every_refusing_status():
    """Five entries, and a sixth status tomorrow fails here instead of in production.

    ``refused_by`` carries a status, never an HTTP string -- so the only thing
    standing between a new status and a ``KeyError`` inside a request is this
    assertion.
    """
    assert set(REFUSAL_CODE) == set(InvoiceStatus) - {InvoiceStatus.CREATED}


def test_created_is_not_in_the_refusal_table():
    """It cannot be a refusal, so an entry for it would document the impossible.

    Every refusal in ``app.domain.transitions`` is raised on a status that is
    not ``created``: the admission handler refuses only when the resolve is
    something else, and the verdict and confirmation handlers only when the
    snapshot is.
    """
    assert InvoiceStatus.CREATED not in REFUSAL_CODE


def test_the_codes_are_the_ones_the_product_is_promised():
    """Pinned literally, because this table is the contract of TOR section 8.

    Renaming a code silently is an API break for a consumer that branches on
    the string.
    """
    assert REFUSAL_CODE == {
        InvoiceStatus.AWAITING_CONFIRMATIONS: "slot_occupied",
        InvoiceStatus.ATTEMPTS_EXHAUSTED: "attempts_exhausted",
        InvoiceStatus.CONFIRMED: "invoice_already_confirmed",
        InvoiceStatus.EXPIRED: "invoice_expired",
        InvoiceStatus.STALLED: "invoice_stalled",
    }


# ==========================================================================
# Authorization -- three axes
# ==========================================================================


@pytest.mark.parametrize(
    "headers",
    [
        {},  # no header at all
        {"Authorization": ""},  # present and empty
        {"Authorization": "Bearer"},  # scheme, no token
        {"Authorization": "Bearer "},  # scheme, empty token
        {"Authorization": f"Basic {TOKEN}"},  # wrong scheme
        {"Authorization": TOKEN},  # bare token, no scheme
        {"Authorization": f"Bearer {TOKEN[:-1]}"},  # one character short
        {"Authorization": f"Bearer {TOKEN} "},  # trailing space
        {"Authorization": f"Bearer {TOKEN.upper()}"},  # wrong case
    ],
)
async def test_every_bad_credential_is_one_indistinguishable_401(headers: dict[str, str]):
    """Same code for all of them: naming the broken part aims the next guess.

    The non-ASCII case is here because ``compare_digest`` raises ``TypeError``
    on non-ASCII ``str`` -- unencoded, that request would be a 500 rather than
    a refusal, and a 500 is an invitation to keep trying.
    """
    async with client_for(StubSession()) as http:
        response = await http.get(f"/api/v1/invoices/{uuid.uuid4()}", headers=headers)

    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}


async def test_a_latin1_token_is_refused_rather_than_crashing():
    """Built from a raw ASGI scope, because no HTTP client will send it.

    httpx encodes header values as ASCII and refuses this request outright, so
    it cannot be produced through the test client -- but HTTP headers are
    latin-1 on the wire and Starlette decodes them as such, so curl or any raw
    socket can send it and this service will see it.

    It matters because ``hmac.compare_digest`` raises ``TypeError`` on a
    non-ASCII ``str``. Unencoded, this request would be a 500 -- and a 500 on a
    credential check is an invitation to keep guessing. Both sides are encoded
    before comparison, so it is an ordinary refusal.
    """
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/invoices",
        "headers": [(b"authorization", "Bearer tok\u00e9n".encode("latin-1"))],
    }

    with pytest.raises(ApiError) as raised:
        await require_service_token(Request(scope), make_settings())

    assert raised.value.status_code == 401
    assert raised.value.code == "unauthorized"


async def test_the_configured_token_is_non_blank_and_passes():
    """The other half of the pair, and the half that is easy to omit.

    "A wrong token is rejected" passes just as well on an authenticator that
    rejects everything. This asserts the configured credential is not empty and
    that presenting it actually gets through -- here, as far as a 404 for an
    invoice that does not exist, which is proof the request passed the guard.
    """
    settings = make_settings()
    assert settings.SERVICE_TOKEN.strip()

    async with client_for(StubSession()) as http:
        response = await http.get(f"/api/v1/invoices/{uuid.uuid4()}", headers=AUTH)

    assert response.status_code == 404
    assert response.json() == {"error": "invoice_not_found"}


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_configured_token_stops_the_service_from_starting(blank: str):
    """The third kind of emptiness, and the dangerous one.

    A missing header and an empty ``Bearer`` are refused above. An empty
    *configured* token is different in kind: compared against an empty header
    it would succeed, so the service would authenticate everybody while looking
    exactly like a working one. It is refused at construction instead.
    """
    with pytest.raises(ValueError, match="SERVICE_TOKEN"):
        make_settings(SERVICE_TOKEN=blank)


# ==========================================================================
# Bodies: what is a schema error and what is not
# ==========================================================================


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"product_ref": "p"},
        {"product_ref": "p", "network": "USDT-ERC20"},
        {"product_ref": "", "network": "USDT-ERC20", "invoice_amount_cents": 1},
        {"product_ref": "p", "network": "", "invoice_amount_cents": 1},
        {"product_ref": "p", "network": "USDT-ERC20", "invoice_amount_cents": 0},
        {"product_ref": "p", "network": "USDT-ERC20", "invoice_amount_cents": -1},
        {"product_ref": "p", "network": "USDT-ERC20", "invoice_amount_cents": 1, "x": 1},
    ],
)
async def test_a_malformed_creation_body_is_422_in_our_own_envelope(body: dict[str, object]):
    """FastAPI's native 422 shape is normalised so the API has one envelope.

    A client branching on ``error`` would otherwise need a second branch for
    ``detail`` -- and the zero-amount case is here because the floor lives in
    the schema rather than as a column CHECK.
    """
    session = StubSession()
    async with client_for(session) as http:
        response = await http.post("/api/v1/invoices", json=body, headers=AUTH)

    assert response.status_code == 422
    assert response.json()["error"] == "invalid_body"
    assert session.commits == 0


async def test_a_validation_error_body_carries_no_submitted_values():
    """Locations and types only.

    Pydantic's raw errors include the offending input. Echoing that back puts
    whatever the caller sent -- a token pasted into the wrong field, say --
    into every log that records error bodies.
    """
    async with client_for(StubSession()) as http:
        response = await http.post(
            "/api/v1/invoices",
            json={"product_ref": "secret-value", "network": "USDT-ERC20"},
            headers=AUTH,
        )

    assert "secret-value" not in response.text
    assert all(set(item) == {"loc", "type"} for item in response.json()["detail"])


@pytest.mark.parametrize("path_value", ["not-a-uuid", "123", "  "])
async def test_a_path_id_that_is_not_a_uuid_is_a_schema_error(path_value: str):
    async with client_for(StubSession()) as http:
        response = await http.get(f"/api/v1/invoices/{path_value}", headers=AUTH)

    assert response.status_code == 422
    assert response.json()["error"] == "invalid_body"


async def test_a_well_formed_but_unknown_id_is_404():
    async with client_for(StubSession()) as http:
        response = await http.get(f"/api/v1/invoices/{uuid.uuid4()}", headers=AUTH)

    assert response.status_code == 404
    assert response.json() == {"error": "invoice_not_found"}


# ==========================================================================
# Networks we do not serve
# ==========================================================================


@pytest.mark.parametrize(
    "network",
    [
        "PoS",  # the product has a fourth network we have no adapter for
        "BEP20",  # the product's spelling of BSC20
        "TRC20",  # the product's spelling, without our prefix
        "usdt-erc20",  # case
        "USDT-ERC20 ",  # trailing space
        " USDT-ERC20",  # leading space
        "USDT-XRP20",  # invented
    ],
)
async def test_an_unserved_network_is_400_and_says_so(network: str):
    """400 with its own code, not 422.

    The body was well-formed; the value is simply one this service does not
    serve. Answered as a schema error it would be indistinguishable from a
    missing field, and the recipient would go and check their JSON instead of
    their network list. The value is echoed back because every likely cause --
    a typo, a rename applied on one side only, a network the product enabled
    before we grew an adapter -- is diagnosed by seeing exactly what arrived.
    """
    session = StubSession()
    async with client_for(session) as http:
        response = await http.post(
            "/api/v1/invoices",
            json={"product_ref": "p", "network": network, "invoice_amount_cents": 100},
            headers=AUTH,
        )

    assert response.status_code == 400
    assert response.json() == {"error": "network_not_supported", "network": network}
    assert session.commits == 0
    assert session.added == []


async def test_an_unserved_network_never_creates_a_row():
    """The reason the check is before the insert and not after.

    The address is snapshotted onto the invoice, so a row created with a bad
    one is a payment instruction to nowhere that outlives every later fix to
    config. Nothing is added and nothing is committed.
    """
    session = StubSession()
    async with client_for(session) as http:
        await http.post(
            "/api/v1/invoices",
            json={"product_ref": "p", "network": "PoS", "invoice_amount_cents": 100},
            headers=AUTH,
        )

    assert session.added == []
    assert session.commits == 0


@pytest.mark.parametrize("network", ["USDT-TRC20", "USDT-ERC20", "USDT-BSC20"])
async def test_every_served_network_gets_its_configured_address_snapshotted(network: str):
    session = StubSession()
    async with client_for(session) as http:
        response = await http.post(
            "/api/v1/invoices",
            json={"product_ref": "p", "network": network, "invoice_amount_cents": 100},
            headers=AUTH,
        )

    assert response.status_code == 201
    expected = TRON_WALLET if network == "USDT-TRC20" else EVM_WALLET
    assert response.json()["address"] == expected
    assert response.json()["status"] == "created"


# ==========================================================================
# The format gate, seen from the route
# ==========================================================================


@pytest.mark.parametrize("txid", ["", "   ", "0x", "0x" + "ab" * 31, "ab" * 32])
async def test_a_malformed_txid_answers_200_and_reaches_no_explorer(txid: str):
    """``invalid_format`` is a documented result code, not a schema error.

    TOR section 8 lists it among the codes of a successful response, so
    answering 422 would move a documented outcome into a different status
    class. Nothing is written and no call is made -- the transport is asserted
    to have served nobody.
    """
    row = invoice()
    session = StubSession({row.id: row})
    transport = RecordingTransport()

    async with client_for(session, transport) as http:
        response = await http.post(
            f"/api/v1/invoices/{row.id}/txid", json={"txid": txid}, headers=AUTH
        )

    assert response.status_code == 200
    assert response.json()["result_code"] == "invalid_format"
    assert response.json()["status"] == "created"
    assert transport.calls == 0
    assert session.commits == 0


async def test_a_missing_txid_field_is_a_schema_error_unlike_an_empty_one():
    """The distinction the previous test depends on.

    An empty string is a value we can classify; an absent field is a body we
    cannot read. One is 200 with ``invalid_format``, the other is 422.
    """
    row = invoice()
    async with client_for(StubSession({row.id: row})) as http:
        response = await http.post(f"/api/v1/invoices/{row.id}/txid", json={}, headers=AUTH)

    assert response.status_code == 422
    assert response.json()["error"] == "invalid_body"


async def test_an_api_error_from_the_explorer_costs_nothing():
    """No row, no attempt, no status change -- and the attempt budget untouched."""
    row = invoice()
    session = StubSession({row.id: row})
    transport = RecordingTransport(*[json_response({}, status_code=500) for _ in range(1)])

    async with client_for(session, transport) as http:
        response = await http.post(
            f"/api/v1/invoices/{row.id}/txid", json={"txid": EVM_TXID}, headers=AUTH
        )

    assert response.status_code == 200
    body = response.json()
    assert body["result_code"] == "api_error"
    assert body["status"] == "created"
    assert body["attempts_used"] == 0
    assert body["attempts_remaining"] == 3
    assert session.commits == 0


async def test_the_transaction_is_released_before_the_explorer_is_called():
    """P-24, the half that does not need a database.

    The retry series takes up to seven seconds, and a pooled connection held
    across it sits idle in transaction for the whole window -- which caps
    concurrent submissions at the size of the pool. The route now releases it
    first, exactly as the confirmations worker does.

    This half proves the call was made and that it came before the explorer was
    reached. It cannot prove the call had the effect it is supposed to have:
    that needs a real transaction and lives in ``test_routes_db``. Either half
    alone would go green on something broken.
    """
    row = invoice()
    session = StubSession({row.id: row})
    rollbacks_when_the_explorer_answered: list[int] = []

    def note(_request: httpx.Request) -> httpx.Response:
        rollbacks_when_the_explorer_answered.append(session.rollbacks)
        # An answer the explorer layer turns into api_error: it keeps this test
        # on the question it is about, with no attempt row and no savepoint.
        return json_response({}, status_code=500)

    transport = httpx.MockTransport(note)

    async with client_for(session, transport) as http:  # type: ignore[arg-type]
        await http.post(
            f"/api/v1/invoices/{row.id}/txid", json={"txid": EVM_TXID}, headers=AUTH
        )

    assert rollbacks_when_the_explorer_answered
    assert set(rollbacks_when_the_explorer_answered) == {1}


async def test_a_refusal_reaches_no_explorer_and_so_releases_nothing():
    """The paired negative: the release belongs to the path that has a call.

    A refusal commits and answers without ever leaving the process, so a
    rollback there would be a transaction thrown away for nothing. Without this
    test, a rollback moved to the top of the handler would satisfy the previous
    one just as well.
    """
    row = invoice(ttl_minutes=-1)
    session = StubSession({row.id: row})

    async with client_for(session) as http:
        response = await http.post(
            f"/api/v1/invoices/{row.id}/txid", json={"txid": EVM_TXID}, headers=AUTH
        )

    assert response.status_code == 409
    assert session.rollbacks == 0


# ==========================================================================
# Refusals, and the writes some of them carry
# ==========================================================================


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (InvoiceStatus.CONFIRMED, "invoice_already_confirmed"),
        (InvoiceStatus.EXPIRED, "invoice_expired"),
        (InvoiceStatus.STALLED, "invoice_stalled"),
        (InvoiceStatus.ATTEMPTS_EXHAUSTED, "attempts_exhausted"),
    ],
)
async def test_each_refusing_status_answers_its_own_code(status: InvoiceStatus, code: str):
    """Five separate cases rather than one parametrised "everything else".

    The fifth, ``awaiting_confirmations``, is below: it needs a frozen slot to
    be a legal row at all, so it cannot share this table.
    """
    row = invoice(status=status, slot_frozen_at=datetime.now(UTC))
    session = StubSession({row.id: row})
    transport = RecordingTransport()

    async with client_for(session, transport) as http:
        response = await http.post(
            f"/api/v1/invoices/{row.id}/txid", json={"txid": EVM_TXID}, headers=AUTH
        )

    assert response.status_code == 409
    assert response.json() == {"error": code}
    assert transport.calls == 0


async def test_a_foreign_txid_against_a_held_slot_is_slot_occupied():
    row = invoice(
        status=InvoiceStatus.AWAITING_CONFIRMATIONS,
        active_txid="0x" + "cd" * 32,
        slot_frozen_at=datetime.now(UTC),
    )
    session = StubSession({row.id: row})
    transport = RecordingTransport()

    async with client_for(session, transport) as http:
        response = await http.post(
            f"/api/v1/invoices/{row.id}/txid", json={"txid": EVM_TXID}, headers=AUTH
        )

    assert response.status_code == 409
    assert response.json() == {"error": "slot_occupied"}
    assert transport.calls == 0


async def test_an_expired_ttl_is_refused_and_the_resolve_is_written():
    """A refusal that carries a status change, which is easy to drop.

    Answering 409 without persisting would leave the invoice in ``created``
    past its deadline until something else touched it, and the product would
    never get the ``expired`` event.
    """
    row = invoice(ttl_minutes=-1)
    session = StubSession({row.id: row})

    async with client_for(session) as http:
        response = await http.post(
            f"/api/v1/invoices/{row.id}/txid", json={"txid": EVM_TXID}, headers=AUTH
        )

    assert response.status_code == 409
    assert response.json() == {"error": "invoice_expired"}
    assert row.status == "expired"
    assert session.commits == 1


async def test_a_budget_lowered_under_a_live_invoice_refuses_and_writes():
    """``attempts_used`` can already exceed a freshly lowered ceiling.

    The refusal comes with ``next_status = attempts_exhausted``, and dropping
    that write would leave a ``created`` invoice that refuses every submission
    forever without ever saying why to the product.
    """
    row = invoice(attempts_used=5)
    session = StubSession({row.id: row})

    async with client_for(session) as http:
        response = await http.post(
            f"/api/v1/invoices/{row.id}/txid", json={"txid": EVM_TXID}, headers=AUTH
        )

    assert response.status_code == 409
    assert response.json() == {"error": "attempts_exhausted"}
    assert row.status == "attempts_exhausted"
    assert session.commits == 1


# ==========================================================================
# The read route writes nothing
# ==========================================================================


async def test_reading_an_invoice_never_commits():
    """Observed, not promised in a docstring."""
    row = invoice()
    session = StubSession({row.id: row})

    async with client_for(session) as http:
        response = await http.get(f"/api/v1/invoices/{row.id}", headers=AUTH)

    assert response.status_code == 200
    assert session.commits == 0
    assert session.added == []


async def test_an_expired_ttl_reads_as_expired_without_touching_the_row():
    """The status is resolved against the clock and the row is left alone.

    The row still says ``created``; the sweeper is what makes them agree and
    emits the event. So this route can legitimately be ahead of the webhook.
    """
    row = invoice(ttl_minutes=-1)
    session = StubSession({row.id: row})

    async with client_for(session) as http:
        response = await http.get(f"/api/v1/invoices/{row.id}", headers=AUTH)

    assert response.json()["status"] == "expired"
    assert row.status == "created"
    assert session.commits == 0


async def test_reading_twice_gives_the_same_answer():
    """Repeatable because it is pure: no write means no drift between calls."""
    row = invoice(ttl_minutes=-1)
    session = StubSession({row.id: row})

    async with client_for(session) as http:
        first = await http.get(f"/api/v1/invoices/{row.id}", headers=AUTH)
        second = await http.get(f"/api/v1/invoices/{row.id}", headers=AUTH)

    assert first.json() == second.json()
    assert session.commits == 0


async def test_the_read_route_reports_attempts_remaining():
    """Derived, and returned even though TOR section 4 does not list it.

    Without it a client that reloads a page has ``attempts_used`` and nothing
    to subtract from: ``MAX_TXID_ATTEMPTS`` is this service's config and is not
    exposed. It would hard-code three, which is a rule of this service living
    inside the product.
    """
    row = invoice(attempts_used=2)
    session = StubSession({row.id: row})

    async with client_for(session) as http:
        body = (await http.get(f"/api/v1/invoices/{row.id}", headers=AUTH)).json()

    assert body["attempts_used"] == 2
    assert body["attempts_remaining"] == 1


async def test_attempts_remaining_never_goes_negative():
    """A lowered ceiling leaves live invoices above it; the wire shows zero."""
    row = invoice(attempts_used=9)
    session = StubSession({row.id: row})

    async with client_for(session) as http:
        body = (await http.get(f"/api/v1/invoices/{row.id}", headers=AUTH)).json()

    assert body["attempts_remaining"] == 0


async def test_the_read_route_does_not_expose_the_frozen_slot():
    """TOR section 8 marks ``slot_frozen_at`` internal."""
    row = invoice(
        status=InvoiceStatus.AWAITING_CONFIRMATIONS,
        active_txid=EVM_TXID,
        slot_frozen_at=datetime.now(UTC),
    )
    session = StubSession({row.id: row})

    async with client_for(session) as http:
        body = (await http.get(f"/api/v1/invoices/{row.id}", headers=AUTH)).json()

    assert "slot_frozen_at" not in body
    assert body["active_txid"] == EVM_TXID
