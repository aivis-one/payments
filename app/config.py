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

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.addresses import is_evm_address, is_tron_address
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

    # Bearer credential for every inbound route (TOR section 8). Mandatory
    # and validated below: an empty token compared against an empty header
    # authenticates everyone.
    SERVICE_TOKEN: str

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

    # Background worker (P-13/P-14). TOR section 10 names none of these: the
    # worker did not exist when the table was written.
    #
    # The polling schedule backs off with the *age of the slot*, not with a
    # count of failures. Confirmations accrue in minutes -- roughly a minute for
    # 20 TRON blocks, 45 seconds for 15 on BSC, two and a half for 12 on
    # Ethereum -- so a transaction still unconfirmed after an hour is stuck or
    # displaced, and hourly is enough for it. A flat minute for the whole
    # seven-day observation window would be about 10,000 calls per stuck
    # invoice; this is about 190. The age is already on the row as
    # ``slot_frozen_at``, so no failure counter and no fourth column.
    WORKER_TICK_SECONDS: float = 5.0
    WORKER_POLL_MIN_SECONDS: float = 30.0
    WORKER_POLL_MAX_SECONDS: float = 3600.0

    # How long a claimed invoice stays invisible to other workers. Generous
    # against the seven seconds a lookup can take, and short enough that a
    # worker killed mid-poll only strands its invoice for five minutes.
    WORKER_LEASE_SECONDS: float = 300.0

    # How many invoices one tick claims.
    WORKER_BATCH_SIZE: int = 50

    # Explorers.
    #
    # Both base URLs are configurable. TOR section 10 lists only the TronScan
    # one; the omission of the Etherscan URL is an accident of that table
    # rather than a decision, and hard-coding it here would make the accident
    # permanent -- the host is the single knob that lets an operator point the
    # service at a mirror or a testnet gateway without a code change.
    ETHERSCAN_API_KEY: str
    ETHERSCAN_API_URL: str = "https://api.etherscan.io/v2/api"
    TRONSCAN_API_URL: str = "https://apilist.tronscanapi.com/api"

    # Mandatory since TOR section 10 of 2026-08-27. TronScan no longer serves
    # unkeyed callers at a guaranteed rate and answers 401 on some endpoints,
    # so a service without a key works until it is under load and then does
    # not. No default: an absent key must stop the process, not degrade it.
    TRONSCAN_API_KEY: str

    # USDT contracts. Decimals are per-network and are NOT derivable from one
    # formula: Binance-Peg BSC-USD carries 18 decimals, not 6 (TOR section 6b).
    USDT_CONTRACT_ERC20: str = "0xdAC17F958D2ee523a2206206994597C13D831ec7"
    USDT_CONTRACT_BSC20: str = "0x55d398326f99059fF775485246999027B3197955"
    USDT_CONTRACT_TRC20: str = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
    USDT_DECIMALS_TRC20: int = 6
    USDT_DECIMALS_ERC20: int = 6
    USDT_DECIMALS_BSC20: int = 18

    @model_validator(mode="after")
    def _refuse_to_boot_misconfigured(self) -> Settings:
        """Fail closed on credentials and addresses, at import rather than in use.

        Both checks guard values that pydantic accepts happily: a mandatory
        ``str`` field is satisfied by ``""``, so every secret in this class can
        be blank without anything complaining.

        **The token.** An empty ``SERVICE_TOKEN`` compared against an empty
        ``Authorization`` header succeeds, so a blank value does not lock the
        service down -- it opens it to everyone. That failure is silent and
        looks exactly like working authentication.

        **The addresses.** This is the one that is silent *and* permanent. An
        invoice snapshots the configured address onto its own row (TOR section
        4), so a blank or malformed value does not merely break the next
        payment: it is copied into the database and survives any later fix to
        the environment. Every invoice issued in that window is a payment
        instruction to nowhere, and no amount of correcting config afterwards
        repairs one.

        Checked per field rather than per network on purpose. The field name
        already carries the network, so no fourth copy of the network-name
        table is needed to know which shape applies.

        Format, not just emptiness: a value can be non-blank and still be
        unusable. ``TCiTestTronAddressForCiOnly000000000`` sat in this
        project's own CI for two deliveries -- 36 characters instead of 34,
        containing ``0``, ``O`` and ``l``, which base58 does not have.
        """
        if not self.SERVICE_TOKEN.strip():
            raise ValueError("SERVICE_TOKEN must not be blank")

        malformed = [
            name
            for name, value, ok in (
                ("WALLET_ADDRESS_USDT_TRC20", self.WALLET_ADDRESS_USDT_TRC20, is_tron_address),
                ("WALLET_ADDRESS_USDT_ERC20", self.WALLET_ADDRESS_USDT_ERC20, is_evm_address),
                ("WALLET_ADDRESS_USDT_BSC20", self.WALLET_ADDRESS_USDT_BSC20, is_evm_address),
            )
            if not ok(value)
        ]
        if malformed:
            raise ValueError(f"not usable wallet addresses: {', '.join(malformed)}")

        return self

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
