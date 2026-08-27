"""One error envelope for the whole API, and the one refusal table.

**Why every error looks the same.** The product branches on ``error``. If some
failures came back as ``{"error": ...}`` and others as FastAPI's native
``{"detail": [...]}``, that branch would have to know which is which, and a
client with one generic handler for 422 would lump "your JSON is malformed"
together with "we do not serve that network" -- two failures with completely
different fixes. So ``RequestValidationError`` is normalised into the same
shape here, and an unsupported network is answered with 400 rather than 422 so
that the status code separates them as well as the body does.

**Why the refusal table lives here and only here.** ``Decision.refused_by``
carries an :class:`InvoiceStatus`, never an HTTP string -- that is stated in
``app.domain.transitions`` and is the reason the domain holds no copy of TOR
section 8's table. This module is the copy. Keying it on the enum rather than
on strings means a typo does not compile, and the totality test in
``tests/test_routes.py`` means a seventh status added tomorrow fails a test
instead of producing a 500 in production.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import UnknownNetworkError
from app.domain.statuses import InvoiceStatus


class ApiError(Exception):
    """An answer the client is meant to read and act on.

    Not an internal failure: those stay exceptions and become 500s, which is
    the correct answer to a bug.
    """

    def __init__(self, status_code: int, code: str, **extra: Any) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.extra = extra

    def as_response(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status_code, content={"error": self.code, **self.extra}
        )


#: TOR section 8: the five statuses that refuse a TXID submission, and the code
#: each one answers with. ``created`` is absent because ``refused_by`` can never
#: be ``created`` -- every refusal in ``app.domain.transitions`` is raised on a
#: status that is not it.
REFUSAL_CODE: dict[InvoiceStatus, str] = {
    InvoiceStatus.AWAITING_CONFIRMATIONS: "slot_occupied",
    InvoiceStatus.ATTEMPTS_EXHAUSTED: "attempts_exhausted",
    InvoiceStatus.CONFIRMED: "invoice_already_confirmed",
    InvoiceStatus.EXPIRED: "invoice_expired",
    InvoiceStatus.STALLED: "invoice_stalled",
}


def refusal(status: InvoiceStatus) -> ApiError:
    """Turn a refusing status into its 409.

    A ``KeyError`` here would mean the domain produced a refusal this layer has
    no answer for, which is a bug rather than a client error -- so it is left
    to raise rather than papered over with a default code.
    """
    return ApiError(409, REFUSAL_CODE[status])


def unauthorized() -> ApiError:
    """One code for every authentication failure.

    Missing header, wrong scheme, empty token and wrong token all answer the
    same thing: telling a caller *which* part of their credential was wrong is
    telling an attacker where to aim.
    """
    return ApiError(401, "unauthorized")


def not_found() -> ApiError:
    return ApiError(404, "invoice_not_found")


def network_not_supported(network: str) -> ApiError:
    """400, and deliberately not 422.

    An unsupported network is not a malformed request -- the body was perfectly
    well-formed and the value is simply one this service does not serve. Given
    422 with FastAPI's own body, it would be indistinguishable from a missing
    field, and whoever received it would go and check their JSON instead of
    their network list.

    The offending value is echoed back because the likely causes are a typo, a
    rename that landed on one side only, and a network the product enabled
    before this service grew an adapter for it -- all three are diagnosed by
    seeing exactly what arrived.
    """
    return ApiError(400, "network_not_supported", network=network)


async def api_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return exc.as_response()


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Normalise FastAPI's own 422 into the envelope everything else uses."""
    assert isinstance(exc, RequestValidationError)
    return JSONResponse(
        status_code=422,
        content={"error": "invalid_body", "detail": _plain(exc.errors())},
    )


async def unknown_network_handler(request: Request, exc: Exception) -> JSONResponse:
    """Backstop for ``UnknownNetworkError`` escaping any config lookup.

    The invoice route checks the network up front, so this should not fire from
    there. It exists because the config boundary raises this from three
    different methods, and an unconfigured network reaching any of them is a
    client-side fact, not a server fault -- answering 500 would send the caller
    to read our logs instead of their own network list.
    """
    assert isinstance(exc, UnknownNetworkError)
    return network_not_supported(str(exc)).as_response()


def _plain(errors: Sequence[Any]) -> list[dict[str, Any]]:
    """Strip pydantic's error objects down to what is safe to serialise.

    ``ctx`` can carry the original exception instance, which is not JSON, and
    ``input`` can carry the raw submitted value -- echoing that back into an
    error body is how a bad secret ends up in somebody's log aggregator.
    """
    return [
        {"loc": [str(part) for part in error.get("loc", ())], "type": error.get("type", "")}
        for error in errors
    ]
