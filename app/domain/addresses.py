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

**The two predicates are not equally strong, and the gap is on the EVM side.**

``is_tron_address`` verifies the base58check checksum: one mistyped or
transposed character fails it mathematically, which is what makes it usable
against a value pasted by hand into an installer prompt.

``is_evm_address`` checks length and alphabet only. It cannot do better without
keccak-256, which is not in the standard library (``hashlib.sha3_256`` is a
different function), and adding a dependency for it was weighed and refused.
The consequence, stated plainly because a reader will otherwise assume the two
sides are symmetric: **an ERC20 or BSC20 address with one wrong character
passes this check and is snapshotted onto invoices.**

EIP-55 would not have closed that gap on its own either. The checksum lives in
the *letter case* of a mixed-case address, and an all-lowercase address is
valid and carries no checksum at all -- so catching a typo would additionally
require refusing the all-lowercase form, which is a policy about what operators
may paste rather than a fact about the chain. Written down here so that the
next reader does not rediscover it as an oversight.

The regular expressions below stay shape-only on purpose. They are what the
explorer adapters use on data from the wire, where addresses arrive
lower-cased and where a shape check is exactly the question being asked; the
predicates are what the config validator uses on operator input. One name for
two properties is what these two layers must not become.
"""

from __future__ import annotations

import hashlib
import re
from typing import Final

#: Ethereum and BSC: ``0x`` plus twenty bytes of hex, either case.
EVM_ADDRESS: Final[re.Pattern[str]] = re.compile(r"0x[0-9a-fA-F]{40}")

#: TRON: ``T`` plus thirty-three base58 characters. The character class is the
#: base58 alphabet with ``0``, ``O``, ``I`` and ``l`` removed.
TRON_ADDRESS: Final[re.Pattern[str]] = re.compile(r"T[1-9A-HJ-NP-Za-km-z]{33}")

#: The base58 alphabet, in value order.
BASE58_ALPHABET: Final[str] = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

#: Mainnet TRON addresses decode to twenty-one bytes starting with this one.
TRON_VERSION_BYTE: Final[int] = 0x41

#: Twenty-one payload bytes plus four checksum bytes.
TRON_DECODED_LENGTH: Final[int] = 25


def _base58_decode(value: str) -> bytes | None:
    """Decode base58 into exactly ``TRON_DECODED_LENGTH`` bytes, or None.

    Leading zero bytes would normally be carried by leading ``1`` characters,
    and that general rule is deliberately not implemented: a mainnet TRON
    address always starts with the version byte ``0x41``, so its decoded form
    never has a leading zero and the fixed width below is exact rather than a
    simplification. A value that does not fit it is not an address.
    """
    number = 0
    for character in value:
        position = BASE58_ALPHABET.find(character)
        if position < 0:
            return None
        number = number * 58 + position
    if number.bit_length() > TRON_DECODED_LENGTH * 8:
        return None
    return number.to_bytes(TRON_DECODED_LENGTH, "big")


def is_evm_address(value: str) -> bool:
    """True when ``value`` is exactly an EVM address, untrimmed.

    Shape only -- see the module docstring for what that does not catch.
    """
    return EVM_ADDRESS.fullmatch(value) is not None


def is_tron_address(value: str) -> bool:
    """True when ``value`` is a TRON address whose base58check checksum holds.

    Three conditions, and all three are load-bearing: the shape, the mainnet
    version byte, and the checksum. The checksum alone would accept a
    well-formed address of another network; the shape alone accepts a typo.
    """
    if TRON_ADDRESS.fullmatch(value) is None:
        return False

    decoded = _base58_decode(value)
    if decoded is None:
        return False

    payload, checksum = decoded[:-4], decoded[-4:]
    if payload[0] != TRON_VERSION_BYTE:
        return False

    return hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] == checksum
