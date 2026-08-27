"""raw_to_cents: floor conversion, per-network decimals, boundary inputs."""

from __future__ import annotations

import pytest

from app.config import Settings, UnknownNetworkError
from app.domain.amounts import raw_to_cents

UINT256_MAX = 2**256 - 1


def test_six_decimals():
    assert raw_to_cents(100_000_000, 6) == 10_000


def test_eighteen_decimals():
    """Binance-Peg BSC-USD: same nominal amount, sixteen orders of magnitude apart."""
    assert raw_to_cents(10**18, 18) == 100


def test_same_raw_means_different_money_on_different_networks():
    """The reason there is no single formula across networks (T-06)."""
    raw = 1_000_000

    assert raw_to_cents(raw, 6) == 100
    assert raw_to_cents(raw, 18) == 0


def test_zero_is_zero():
    assert raw_to_cents(0, 6) == 0
    assert raw_to_cents(0, 18) == 0


def test_uint256_max_is_exact_and_does_not_raise():
    """Python integers are unbounded, so nothing overflows here.

    The bigint ceiling of the credited_amount_cents column is a separate,
    downstream question (T-32) and is deliberately not enforced in this pure
    function.
    """
    assert raw_to_cents(UINT256_MAX, 6) == UINT256_MAX // 10**4
    assert raw_to_cents(UINT256_MAX, 6) > 10**60


@pytest.mark.parametrize(
    ("raw", "decimals", "expected"),
    [
        (9_999_999, 6, 999),  # 9.999999 USDT -> 999 cents, not 1000
        (100_009_999, 6, 10_000),
        (1, 6, 0),
        (9_999, 6, 0),
        (10**18 + 10**16 - 1, 18, 100),
    ],
)
def test_floor_never_rounds_up(raw: int, decimals: int, expected: int):
    """Rounding up would credit money that never arrived at the address."""
    assert raw_to_cents(raw, decimals) == expected


def test_repeated_conversion_of_the_same_raw_is_stable():
    """123.456789 USDT -> 12345 cents, and the same on every call."""
    assert raw_to_cents(123_456_789, 6) == raw_to_cents(123_456_789, 6) == 12_345


def test_negative_raw_is_rejected():
    """A negative on-chain amount is an adapter contract violation.

    Flooring it silently would produce a negative credit: -1 // 10**4 is -1.
    """
    with pytest.raises(ValueError, match="non-negative"):
        raw_to_cents(-1, 6)


def test_decimals_below_two_is_rejected():
    with pytest.raises(ValueError, match="decimals"):
        raw_to_cents(100, 1)


def _settings() -> Settings:
    """Settings for the policy tests.

    The placeholder addresses used to be ``"T1"`` and ``"0x1"`` -- short enough
    to read, and impossible on any chain. H3 made ``Settings`` refuse to build
    on a wallet address that is not shaped like a real one, because an invoice
    snapshots that value onto its own row and a malformed one survives every
    later fix to the environment. These tests are about ``policy_for``, not
    about addresses, so they now carry real shapes and say nothing else about
    them.
    """
    return Settings(
        DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
        SERVICE_TOKEN="token",
        WALLET_ADDRESS_USDT_TRC20="TMuA6YqfCeX8EhbfYEg5y7S4DqzSJireY9",
        WALLET_ADDRESS_USDT_ERC20="0x0000000000000000000000000000000000000001",
        WALLET_ADDRESS_USDT_BSC20="0x0000000000000000000000000000000000000002",
        ETHERSCAN_API_KEY="key",
    )


def test_policy_resolves_decimals_per_network():
    settings = _settings()

    assert settings.policy_for("USDT-TRC20").decimals == 6
    assert settings.policy_for("USDT-ERC20").decimals == 6
    assert settings.policy_for("USDT-BSC20").decimals == 18


def test_policy_resolves_confirmations_per_network():
    settings = _settings()

    assert settings.policy_for("USDT-TRC20").confirmations_required == 20
    assert settings.policy_for("USDT-ERC20").confirmations_required == 12
    assert settings.policy_for("USDT-BSC20").confirmations_required == 15


def test_unknown_network_fails_at_the_config_edge():
    """The only place an unknown network exists; the function never sees one."""
    with pytest.raises(UnknownNetworkError):
        _settings().policy_for("USDT-SOMETHING")


def test_ttl_is_not_part_of_the_policy():
    """The deadline lives on the invoice row, not in config-at-decision-time."""
    policy = _settings().policy_for("USDT-ERC20")

    assert not hasattr(policy, "invoice_ttl_minutes")
    assert set(policy.__slots__) == {
        "decimals",
        "confirmations_required",
        "max_txid_attempts",
        "max_observation_window",
    }
