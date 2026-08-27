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
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from app.api.errors import (
    ApiError,
    api_error_handler,
    unknown_network_handler,
    validation_error_handler,
)
from app.api.routes import router
from app.config import UnknownNetworkError


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
