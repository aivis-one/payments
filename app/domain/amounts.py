"""Raw explorer amounts to integer cents.

This is the only place in the service where a raw on-chain amount is divided.
Explorer adapters (H2) hand over ``raw_amount`` exactly as the explorer
reported it -- no division, no rounding, no float anywhere -- so that the floor
rule of TOR section 6b lives in one function and can be wrong in one place at
most.
"""

from __future__ import annotations


def raw_to_cents(raw_amount: int, decimals: int) -> int:
    """Convert a raw token amount into integer cents, rounding down.

    ``cents = raw_amount // 10 ** (decimals - 2)`` (TOR section 6b).

    Floor, never round-half-up: rounding up would occasionally credit more than
    physically arrived at the address, i.e. create money that is not there.
    Sub-cent dust is lost, which is an arithmetic limitation and not a business
    decision.

    Decimals are per-network and there is no single formula: USDT on Ethereum
    and Tron carry 6 decimals, Binance-Peg BSC-USD carries 18.

    No upper bound is enforced. Python integers are unbounded, so
    ``uint256``-sized input returns an exact value here. The persistence
    ceiling is a different question and a different task: ``bigint`` tops out
    near 9.2e18 and the check belongs where the value is written (see the
    delivery report, T-32).

    Raises:
        ValueError: if ``raw_amount`` is negative or ``decimals`` is below 2.
            Both are contract violations of the adapter layer, not domain
            states -- a negative on-chain amount does not exist, and silently
            flooring it toward negative infinity would produce a negative
            credit.
    """
    if raw_amount < 0:
        raise ValueError(f"raw_amount must be non-negative, got {raw_amount}")
    if decimals < 2:
        raise ValueError(f"decimals must be >= 2, got {decimals}")
    divisor: int = 10 ** (decimals - 2)
    return raw_amount // divisor
