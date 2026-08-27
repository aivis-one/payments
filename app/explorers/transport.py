"""The one place that decides when an explorer failed to answer at all.

Both adapters route every call through :func:`fetch_json`, so the boundary
between "the explorer answered something about this transaction" and "the
explorer did not answer" is defined once. It is worth defining once because
that boundary is the money: TOR section 7 spends a user attempt on the first
kind of answer and spends nothing on the second.

An empty body with a ``200`` belongs on the failure side. It is tempting to
read it as "nothing found", but an explorer that has nothing to say about a
hash says so in JSON; a zero-length body is a proxy, a truncated response or a
maintenance page, and charging a user an attempt for one would be charging
them for our infrastructure.
"""

from __future__ import annotations

import json
from typing import Final

import httpx

#: Seconds. Long enough for an explorer under load, short enough that three
#: retries plus their backoff stay inside the synchronous request budget of
#: TOR section 7.
DEFAULT_TIMEOUT: Final[float] = 10.0


class ExplorerUnavailable(Exception):
    """The explorer gave no readable answer. Becomes ``api_error``.

    Carries a short reason for logs. The reason is never shown to the user and
    never persisted -- ``api_error`` does not create an attempt row at all.
    """


async def fetch_json(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, str],
    *,
    headers: dict[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> object:
    """GET ``url`` and return the decoded body.

    The return type is :class:`object`, not ``dict``: what comes back is
    whatever the far end sent, and every caller has to narrow it explicitly.
    An annotation of ``dict`` here would be a promise this function cannot
    keep and would move the ``[]``/``null``/string cases from a visible
    ``isinstance`` check into an invisible crash.

    Raises:
        ExplorerUnavailable: on transport failure, a non-2xx status, an empty
            body, or a body that is not JSON.
    """
    try:
        response = await client.get(url, params=params, headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        # Covers connect, read, write, timeout and protocol errors in one
        # branch: from here they are indistinguishable and all mean the same
        # thing to the caller.
        raise ExplorerUnavailable(f"transport failure: {type(exc).__name__}") from exc

    if response.status_code < 200 or response.status_code >= 300:
        raise ExplorerUnavailable(f"http {response.status_code}")

    if not response.content:
        raise ExplorerUnavailable("empty body")

    try:
        return json.loads(response.content)
    except ValueError as exc:
        raise ExplorerUnavailable("body is not json") from exc
