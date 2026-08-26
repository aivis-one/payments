"""Policy: the configuration values the transition function is allowed to see.

Four scalars, resolved by the caller from :class:`app.config.Settings`. The
function itself never touches config, the database, the network or the system
clock.

Scalars, not a ``network -> value`` map. ``network`` is an open list of strings
by design (TOR section 12); a map inside the function would create an "unknown
network" refusal -- a state that appears in none of the four event tables. The
unknown-network boundary belongs at the config edge, where it exists as
``UnknownNetworkError``.

``INVOICE_TTL_MINUTES`` is deliberately NOT here. The TTL is materialised into
``invoices.expires_at`` at creation time and the function compares against that
column. If the minutes value entered the function, the deadline would have two
sources -- the invoice row and the current config -- and editing the config
would silently move the deadline of invoices that were already issued. Same
reasoning as the address snapshot in TOR section 4.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True, slots=True)
class Policy:
    """Resolved, per-network policy values."""

    decimals: int
    confirmations_required: int
    max_txid_attempts: int
    max_observation_window: timedelta

    def __post_init__(self) -> None:
        # These are misconfigurations, not domain states: they are reachable
        # from the environment, so they are rejected at construction rather
        # than turned into branches of the transition function.
        if self.decimals < 2:
            raise ValueError(f"decimals must be >= 2, got {self.decimals}")
        if self.confirmations_required < 1:
            raise ValueError(
                f"confirmations_required must be >= 1, got {self.confirmations_required}"
            )
        if self.max_txid_attempts < 1:
            raise ValueError(f"max_txid_attempts must be >= 1, got {self.max_txid_attempts}")
        if self.max_observation_window <= timedelta(0):
            raise ValueError(
                f"max_observation_window must be positive, got {self.max_observation_window}"
            )
