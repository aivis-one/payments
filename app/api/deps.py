"""Request-scoped dependencies: settings, session, explorer client, auth.

All four are ``Depends`` rather than module-level lookups so that tests can
override them one at a time. ``get_settings`` in ``app.config`` is
``lru_cache``d process-wide; a test that mutated that cache would leak into
every test after it, so tests override this dependency instead and leave the
cache alone.
"""

from __future__ import annotations

import hmac
from typing import Annotated

import httpx
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import unauthorized
from app.config import Settings, get_settings
from app.db import get_session


def settings_dependency() -> Settings:
    """The process-wide settings, as a dependency so tests can replace them."""
    return get_settings()


def explorer_client(request: Request) -> httpx.AsyncClient:
    """The application-scoped HTTP client used to reach explorers.

    One client for the process, opened in the lifespan: a client per request
    would throw away connection pooling and TLS sessions on an endpoint that
    can make four calls inside one request.
    """
    client: httpx.AsyncClient = request.app.state.explorer_client
    return client


SettingsDep = Annotated[Settings, Depends(settings_dependency)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
ClientDep = Annotated[httpx.AsyncClient, Depends(explorer_client)]


async def require_service_token(request: Request, settings: SettingsDep) -> None:
    """Check the bearer credential of TOR section 8.

    The header is parsed by hand rather than with ``HTTPBearer`` because that
    helper answers 403 with its own body shape, and this API has one envelope.

    ``compare_digest`` rather than ``==``: the comparison is against a secret,
    and a short-circuiting comparison leaks its length and prefix through
    timing. Both sides are encoded first -- ``compare_digest`` raises on
    non-ASCII strings, and a token with a stray unicode character would
    otherwise turn an authentication failure into a 500.

    There is no "no token configured" branch. ``Settings`` refuses to build on
    a blank ``SERVICE_TOKEN``, so by the time this runs the configured value is
    non-empty -- which is what stops the empty-header-against-empty-secret case
    from quietly authenticating everybody. That guarantee is asserted from both
    ends in the tests: a wrong token is rejected, *and* the configured one is
    non-blank and passes.
    """
    header = request.headers.get("authorization")
    if header is None:
        raise unauthorized()

    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise unauthorized()

    if not hmac.compare_digest(token.encode(), settings.SERVICE_TOKEN.encode()):
        raise unauthorized()


AuthDep = Depends(require_service_token)
