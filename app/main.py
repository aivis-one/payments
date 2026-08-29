"""ASGI entrypoint.

The explorer HTTP client is opened once for the process rather than per
request: submitting a TXID can make four calls inside one request, and a fresh
client each time would discard connection pooling and TLS sessions exactly
where they matter most.

Three exception handlers, and they exist so that the API has one error shape.
FastAPI's native ``422`` body is a different shape from everything else here,
so it is normalised; ``UnknownNetworkError`` would otherwise escape the config
boundary as a 500 and send the caller to read our logs instead of their own
network list.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI, Response, status
from fastapi.exceptions import RequestValidationError
from sqlalchemy import text

from app.api.deps import SessionDep
from app.api.errors import (
    ApiError,
    api_error_handler,
    unknown_network_handler,
    validation_error_handler,
)
from app.api.routes import router
from app.config import UnknownNetworkError

log = structlog.get_logger("payments.ready")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with httpx.AsyncClient() as client:
        app.state.explorer_client = client
        yield


app = FastAPI(title="payments", version="0.1.0", lifespan=lifespan)

app.add_exception_handler(ApiError, api_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)
app.add_exception_handler(UnknownNetworkError, unknown_network_handler)

app.include_router(router)


@app.get("/ready")
async def ready(session: SessionDep, response: Response) -> dict[str, bool]:
    """Liveness for the deploy contract: is this process able to serve?

    **Outside the router, and therefore outside its bearer credential.** A probe
    that needs a token is a probe docker cannot run, and the container's own
    healthcheck is the first consumer -- the second is ``wait_for_app`` in the
    installer, which reads docker's health state rather than curling from the
    host, because the API publishes no host port.

    **It says up or down and nothing else.** No version, no database name, no
    text of the error. The service is internal, but "internal" is a property of
    today's deploy and not of this handler, and a probe is the one endpoint
    every future reverse proxy is tempted to expose.

    A query, not a connection check: a pool can hand out a connection to a
    database that is no longer answering. It also makes "healthy" imply
    "migrated", because the container runs ``alembic upgrade head`` before
    uvicorn and nothing answers here until that finished.
    """
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        # Deliberately broad and deliberately silent to the caller: every way
        # the database can be unreachable is the same answer to a probe, and
        # the details belong in the container's log, which is where the
        # installer now prints from on failure.
        log.exception("readiness probe failed")
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"ready": False}
    return {"ready": True}
