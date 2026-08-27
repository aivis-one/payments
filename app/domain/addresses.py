"""What a wallet or contract address looks like on each chain.

Lives in the domain rather than next to either user because it has two of them
and they cannot import each other. ``app/explorers/registry.py`` imports
``app.config``, so config may not import the explorers package; and the
adapters may not import the registry, which imports them. A shape that both
sides check has to sit below both, and neither side may hold its own copy --
two spellings of "what an EVM address looks like" is one fact in two places,
and the first person to correct one leaves the other lying.

Two patterns, and the asymmetry between them is real:

* EVM addresses are hex and case-insensitive. EIP-55 uses letter case as a
  checksum, but a lower-cased address is still the same valid address, so
  matching ignores case and comparison normalises to lower.
* TRON addresses are base58 and case-**sensitive**. The alphabet excludes
  ``0``, ``O``, ``I`` and ``l`` precisely so that they cannot be confused, and
  the trailing bytes are a checksum over the exact characters. Changing the
  case of one does not normalise it, it produces a different and invalid
  string.

The TRON pattern is a shape check, not a checksum validation: it catches an
empty value, a truncated one, an EVM address in a TRON slot and characters
base58 does not contain. It deliberately stops short of verifying the checksum,
because a half-implemented validation invites the next reader to trust it as a
whole one.
"""

from __future__ import annotations

import re
from typing import Final

#: Ethereum and BSC: ``0x`` plus twenty bytes of hex, either case.
EVM_ADDRESS: Final[re.Pattern[str]] = re.compile(r"0x[0-9a-fA-F]{40}")

#: TRON: ``T`` plus thirty-three base58 characters. The character class is the
#: base58 alphabet with ``0``, ``O``, ``I`` and ``l`` removed.
TRON_ADDRESS: Final[re.Pattern[str]] = re.compile(r"T[1-9A-HJ-NP-Za-km-z]{33}")


def is_evm_address(value: str) -> bool:
    """True when ``value`` is exactly an EVM address, untrimmed."""
    return EVM_ADDRESS.fullmatch(value) is not None


def is_tron_address(value: str) -> bool:
    """True when ``value`` is exactly a TRON base58 address, untrimmed."""
    return TRON_ADDRESS.fullmatch(value) is not None
