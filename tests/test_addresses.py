"""P-29: what the address predicates catch, and what they do not.

The reason this file exists is the installer. Wallet addresses stopped being
something a developer pastes into a `.env` once and stopped being wrong quietly:
`payments-deploy.sh install` asks a human for three of them at a terminal, and
whatever comes back is snapshotted onto every invoice the service ever issues.
A wrong value is not a broken next payment -- it is copied into the database and
survives every later correction of the config.

**The two sides are not equally protected, and the tests say so out loud.** TRON
is verified by its base58check checksum, so one mistyped character is refused
mathematically. EVM is length and alphabet only. Tests below assert both, the
second one as a negative: writing down a gap is what stops the next reader from
assuming symmetry that is not there.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.addresses import is_evm_address, is_tron_address
from tests.explorers_support import EVM_WALLET, TRON_WALLET, make_settings

pytestmark = pytest.mark.no_network

#: Every TRON address this repository carries, in code, fixtures and CI. They
#: all predate the checksum check, so this list is also the answer to "did
#: tightening the validator break anything that already worked".
KNOWN_GOOD_TRON = [
    TRON_WALLET,
    "TMuA6YqfCeX8EhbfYEg5y7S4DqzSJireY9",  # CI and test_amounts
    "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",  # the USDT contract, config default
    "TLa2f6VPqDgRE67v1736s7bJ8Ray5wYjU7",  # the sender in the TRON fixtures
]


@pytest.mark.parametrize("address", KNOWN_GOOD_TRON)
def test_every_tron_address_already_in_this_repository_still_passes(address: str):
    """The check that had to come before the change, not after it.

    Tightening a validator breaks whatever it now refuses, and the things most
    likely to be refused are the values already sitting in the tree. Run over
    the whole set before a line of the checksum was written; kept as a test so
    that a future address added by hand fails here rather than in CI.
    """
    assert is_tron_address(address)


def test_one_transposed_character_is_refused():
    """The done-when of P-29, and the reason a shape check was not enough.

    Base58 was designed so that a human copying a string cannot silently
    corrupt it -- the alphabet drops the characters that look alike, and the
    last four bytes are a checksum over the rest. A single swap fails it.
    """
    good = TRON_WALLET
    swapped = good[:-3] + good[-2] + good[-3] + good[-1]

    assert good != swapped
    assert is_tron_address(good)
    assert not is_tron_address(swapped)


@pytest.mark.parametrize("position", [1, 5, 17, 33])
def test_one_wrong_character_anywhere_is_refused(position: int):
    """Not just at the end. The checksum covers the whole payload."""
    good = TRON_WALLET
    replacement = "2" if good[position] != "2" else "3"
    broken = good[:position] + replacement + good[position + 1 :]

    assert not is_tron_address(broken)


def test_a_valid_checksum_on_the_wrong_network_is_refused():
    """Shape and checksum are both needed, and neither implies the other.

    A well-formed base58check string of another chain would pass the checksum
    while addressing nothing on TRON, so the mainnet version byte is checked
    too. Built here by taking a real address and changing nothing but its
    leading character -- which also breaks the checksum, and that is the point:
    the two conditions are not separable from the outside, so the test asserts
    the outcome rather than which condition fired.
    """
    assert not is_tron_address("A" + TRON_WALLET[1:])


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        TRON_WALLET[:-1],  # truncated by one, as a paste can be
        TRON_WALLET + "X",  # one too many
        f" {TRON_WALLET}",  # leading space, untrimmed on purpose
        f"{TRON_WALLET} ",
        EVM_WALLET,  # an EVM address in a TRON slot
        "T0000000000000000000000000000000000",  # '0' is not in the alphabet
    ],
)
def test_the_shortfall_and_emptiness_axes_are_refused(value: str):
    assert not is_tron_address(value)


# --------------------------------------------------------------------------
# The EVM side, and the gap in it
# --------------------------------------------------------------------------


def test_an_evm_address_with_one_wrong_character_is_accepted():
    """**A gap, asserted so that nobody rediscovers it as a bug.**

    EIP-55 hides its checksum in the LETTER CASE of a mixed-case address, and
    an all-lowercase address is valid and carries no checksum at all. Catching
    a typo would therefore require both keccak-256 -- absent from the standard
    library, and a dependency the owner declined -- and a policy of refusing
    the all-lowercase form, which is a rule about what operators may paste
    rather than a fact about the chain.

    So an ERC20 or BSC20 address with one wrong character passes, and is
    snapshotted onto invoices. This test fails the day that changes, which is
    the day to delete it.
    """
    good = EVM_WALLET
    broken = good[:-1] + ("0" if good[-1] != "0" else "1")

    assert is_evm_address(good)
    assert is_evm_address(broken)


def test_the_evm_side_still_refuses_shape_errors():
    """The gap is about content, not about shape."""
    assert not is_evm_address("")
    assert not is_evm_address(EVM_WALLET[:-1])
    assert not is_evm_address(EVM_WALLET + "0")
    assert not is_evm_address(EVM_WALLET.replace("0x", "", 1))
    assert not is_evm_address(TRON_WALLET)


def test_case_is_ignored_on_the_evm_side_and_significant_on_the_tron_side():
    """One line that names the asymmetry the two predicates are built on."""
    assert is_evm_address(EVM_WALLET.lower())
    assert is_evm_address(EVM_WALLET.upper().replace("0X", "0x"))
    assert not is_tron_address(TRON_WALLET.lower())


# --------------------------------------------------------------------------
# Through the config, which is where the installer meets it
# --------------------------------------------------------------------------


def test_a_mistyped_wallet_stops_the_service_from_starting():
    """The installer's whole validation strategy, in one assertion.

    There is no second implementation of "what a valid address is" anywhere in
    the deploy scripts. The installer writes the answers and starts the
    container; a bad address makes the container exit here, which the installer
    reports with the tail of its log.
    """
    good = TRON_WALLET
    swapped = good[:-3] + good[-2] + good[-3] + good[-1]

    with pytest.raises(ValidationError, match="WALLET_ADDRESS_USDT_TRC20"):
        make_settings(WALLET_ADDRESS_USDT_TRC20=swapped)


def test_a_valid_configuration_still_builds():
    """The paired positive. Without it the test above passes on a validator
    that refuses everything."""
    settings = make_settings()

    assert is_tron_address(settings.WALLET_ADDRESS_USDT_TRC20)
    assert is_evm_address(settings.WALLET_ADDRESS_USDT_ERC20)
    assert is_evm_address(settings.WALLET_ADDRESS_USDT_BSC20)
