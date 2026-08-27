"""TXID shapes, checked before any explorer is contacted (T-11).

The patterns are named after the shape, not after the network: binding a shape
to a network happens once, in :mod:`app.explorers.registry`, so that a rename
of a network costs one line there and nothing here.

``fullmatch`` is used rather than an anchored ``match`` on purpose. In Python
``$`` also matches immediately before a trailing newline, so an anchored
pattern would accept ``"0x<64 hex>\\n"`` -- a value that is not a TXID and that
a copy-paste out of a terminal produces routinely.

No trimming, no case folding: what arrives is what is checked. The domain
already takes the same position (``test_matched_txid_is_carried_into_the_
effects_verbatim``), and a layer that silently repaired input here would make
the stored TXID differ from the one the user believes they submitted.
"""

from __future__ import annotations

import re
from typing import Final

#: Ethereum and BSC. Identical by construction -- the two chains share a hash
#: format, which is exactly why no regex can tell an ERC20 TXID from a BSC20
#: one and why ``wrong_network`` is unreachable between them.
EVM_TXID: Final[re.Pattern[str]] = re.compile(r"0x[0-9a-fA-F]{64}")

#: TRON. Same 32 bytes, written without the ``0x`` prefix.
TRON_TXID: Final[re.Pattern[str]] = re.compile(r"[0-9a-fA-F]{64}")


def matches(pattern: re.Pattern[str], txid: str) -> bool:
    """True when ``txid`` is exactly the shape ``pattern`` describes."""
    return pattern.fullmatch(txid) is not None
