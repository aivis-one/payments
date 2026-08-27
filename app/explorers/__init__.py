"""Explorer adapters: one submitted TXID becomes one final verdict.

The layer H3 talks to is :func:`app.explorers.verify.verify_txid`. Everything
below it -- which explorer, which envelope, how many internal retries -- is
this package's business and is not part of the surface.

What crosses the boundary is deliberately small:

* a :class:`~app.explorers.protocol.ExplorerResult` carrying a
  :class:`~app.domain.statuses.Verdict`, the undivided ``raw_amount`` on a
  match, and the Transfer sender for
  ``invoice_txid_attempts.from_address``;
* ``UnknownNetworkError`` for a network with no adapter.

No amount is divided anywhere in this package. ``raw_amount`` leaves exactly
as the explorer reported it and is converted by
:func:`app.domain.amounts.raw_to_cents`, which stays the only division in the
service.
"""

from __future__ import annotations

from app.explorers.protocol import ExplorerAdapter, ExplorerResult
from app.explorers.registry import NETWORKS, NetworkSpec, spec_for
from app.explorers.retry import RETRY_DELAYS, resolve
from app.explorers.transport import ExplorerUnavailable
from app.explorers.verify import verify_txid

__all__ = [
    "NETWORKS",
    "RETRY_DELAYS",
    "ExplorerAdapter",
    "ExplorerResult",
    "ExplorerUnavailable",
    "NetworkSpec",
    "resolve",
    "spec_for",
    "verify_txid",
]
