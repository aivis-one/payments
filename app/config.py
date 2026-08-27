"""Service configuration.

Mirrors the parameter table of TOR section 10 one-to-one, plus ``DATABASE_URL``,
which the table does not list but which the service cannot boot or migrate
without.

Config lives here and nowhere else. In particular, the pure transition function
(``app.domain.transitions``) never reads this module: the caller resolves a
``Policy`` via :meth:`Settings.policy_for` and passes it in as a value. Reading
settings inside the transition function would break its purity requirement.
"""

from __future__ import annotations

from datetime import timedelta
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.policy import Policy


class UnknownNetworkError(ValueError):
    """Raised when a network string has no configured decimals/confirmations.

    ``network`` is an open list of strings by design (TOR section 12): adding a
    fourth network is a new adapter plus config, not a model migration. The
    consequence is that an unconfigured network is reachable here, at the
    config boundary -- and only here. The transition function receives resolved
    scalars, so no "unknown network" state exists inside it.
    """


class Settings(BaseSettings):
    """Service settings, sourced from the environment (or a local .env)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Infrastructure. Not part of the TOR section 10 table; required to run.
    DATABASE_URL: str

    # Wallet addresses -- mandatory, no defaults.
    WALLET_ADDRESS_USDT_TRC20: str
    WALLET_ADDRESS_USDT_ERC20: str
    WALLET_ADDRESS_USDT_BSC20: str

    # Confirmation thresholds per network.
    CONFIRMATIONS_REQUIRED_TRC20: int = 20
    CONFIRMATIONS_REQUIRED_ERC20: int = 12
    CONFIRMATIONS_REQUIRED_BSC20: int = 15

    # Invoice lifecycle.
    INVOICE_TTL_MINUTES: int = 60
    MAX_TXID_ATTEMPTS: int = 3
    MAX_OBSERVATION_WINDOW_DAYS: int = 7

    # Explorers.
    #
    # Both base URLs are configurable. TOR section 10 lists only the TronScan
    # one; the omission of the Etherscan URL is an accident of that table
    # rather than a decision, and hard-coding it here would make the accident
    # permanent -- the host is the single knob that lets an operator point the
    # service at a mirror or a testnet gateway without a code change.
    ETHERSCAN_API_KEY: str
    ETHERSCAN_API_URL: str = "https://api.etherscan.io/v2/api"
    TRONSCAN_API_URL: str = "https://apilist.tronscan.org/api"

    # USDT contracts. Decimals are per-network and are NOT derivable from one
    # formula: Binance-Peg BSC-USD carries 18 decimals, not 6 (TOR section 6b).
    USDT_CONTRACT_ERC20: str = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
    USDT_CONTRACT_BSC20: str = "0x55d398326f99059fF775485246999027B3197955"
    USDT_CONTRACT_TRC20: str = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
    USDT_DECIMALS_TRC20: int = 6
    USDT_DECIMALS_ERC20: int = 6
    USDT_DECIMALS_BSC20: int = 18

    def wallet_address_for(self, network: str) -> str:
        """Return the configured wallet address for ``network``.

        The invoice stores a snapshot of this value (TOR section 4); rotating
        the address in config must not break verification of already-open
        invoices, so verification reads the invoice column, never this method.
        """
        addresses = {
            "USDT-TRC20": self.WALLET_ADDRESS_USDT_TRC20,
            "USDT-ERC20": self.WALLET_ADDRESS_USDT_ERC20,
            "USDT-BSC20": self.WALLET_ADDRESS_USDT_BSC20,
        }
        try:
            return addresses[network]
        except KeyError as exc:
            raise UnknownNetworkError(network) from exc

    def policy_for(self, network: str) -> Policy:
        """Resolve config into the four scalars the transition function needs.

        ``INVOICE_TTL_MINUTES`` is deliberately absent from ``Policy``: the TTL
        is materialised into ``invoices.expires_at`` when the invoice is
        created, and the function compares against that column. Letting the
        config value into the function would give the deadline two sources and
        would move the deadline of already-issued invoices whenever the config
        changed.
        """
        decimals = {
            "USDT-TRC20": self.USDT_DECIMALS_TRC20,
            "USDT-ERC20": self.USDT_DECIMALS_ERC20,
            "USDT-BSC20": self.USDT_DECIMALS_BSC20,
        }
        confirmations = {
            "USDT-TRC20": self.CONFIRMATIONS_REQUIRED_TRC20,
            "USDT-ERC20": self.CONFIRMATIONS_REQUIRED_ERC20,
            "USDT-BSC20": self.CONFIRMATIONS_REQUIRED_BSC20,
        }
        try:
            return Policy(
                decimals=decimals[network],
                confirmations_required=confirmations[network],
                max_txid_attempts=self.MAX_TXID_ATTEMPTS,
                max_observation_window=timedelta(days=self.MAX_OBSERVATION_WINDOW_DAYS),
            )
        except KeyError as exc:
            raise UnknownNetworkError(network) from exc


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance."""
    return Settings()  # type: ignore[call-arg]
