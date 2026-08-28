"""Helpers for the delivery half: a stand-in for the product's receiver.

Separate from ``explorers_support`` because it is the other direction of
traffic. The explorer helpers stand in for third-party APIs the service calls
out to and does not trust; this one stands in for the one consumer the service
delivers to.
"""

from __future__ import annotations

import httpx

#: What ``make_settings`` configures as the product's address. ``.invalid`` is
#: a reserved TLD that resolves nowhere, so a request that escaped the mock
#: transport fails instead of reaching something real.
WEBHOOK_URL = "http://product.invalid/webhooks/payments"

#: The secret ``make_settings`` configures, so that a test can assert the
#: header carries it rather than merely being present.
WEBHOOK_SECRET = "test-secret-not-real"


class WebhookTransport(httpx.MockTransport):
    """Answers deliveries from a queue and remembers every request.

    Unlike ``RecordingTransport``, running out of prepared answers is not an
    error: the delivery loop retries, so the number of requests is usually the
    thing under test rather than something the test can state up front. The
    queue's last entry is repeated once it is reached.

    A queue entry may be an exception instead of a response, which is how a
    product that is unreachable rather than unhappy is expressed.
    """

    def __init__(self, *answers: httpx.Response | Exception) -> None:
        self.requests: list[httpx.Request] = []
        self._queue: list[httpx.Response | Exception] = list(answers) or [httpx.Response(200)]
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self._queue.pop(0) if len(self._queue) > 1 else self._queue[0]
        if isinstance(answer, Exception):
            raise answer
        # Responses are consumed once by httpx, so a repeated one is rebuilt.
        return httpx.Response(
            status_code=answer.status_code, content=answer.content, headers=answer.headers
        )

    @property
    def calls(self) -> int:
        return len(self.requests)


def webhook_client(*answers: httpx.Response | Exception) -> httpx.AsyncClient:
    """A client wired to a webhook transport and to nothing else."""
    return httpx.AsyncClient(transport=WebhookTransport(*answers))


def accepting_webhooks() -> httpx.AsyncClient:
    """A product that accepts everything it is sent."""
    return webhook_client(httpx.Response(200))
