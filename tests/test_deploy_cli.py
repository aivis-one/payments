"""P-18: the half of the deploy CLI that can be checked without a daemon.

**What this file can and cannot reach, stated up front.** There is no docker in
CI and none on a developer's machine by default, so everything that ends in a
container -- bringing the stack up, waiting for health, the compose graph -- is
verified by the owner's install on a clean server and by nothing here. What is
left is still the part that decides whether an install is repeatable: secrets
minted once, answers never re-asked, the hand-over writing or printing, and the
one value that decides whether the stack may come up at all.

Those live in shell functions, so the tests call them the way another script
would: source the file, override the four path constants, invoke one function.
The script carries a ``BASH_SOURCE`` guard precisely so that sourcing it does
not also run its dispatch.

Every case works inside ``tmp_path``. Nothing here touches /opt.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.no_network

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "payments-deploy.sh"

#: Values the operator would type. Passed through the environment, which is the
#: path a scripted install takes -- the interactive one cannot be driven from
#: pytest, and faking a terminal would be testing the fake.
ANSWERS = {
    "WALLET_ADDRESS_USDT_TRC20": "TWbdVwjHTNn2PXDPbtSNvvESDd8PpApFmX",
    "WALLET_ADDRESS_USDT_ERC20": "0x742d35Cc6634C0532925a3b844Bc454e4438f44e",
    "WALLET_ADDRESS_USDT_BSC20": "0x742d35Cc6634C0532925a3b844Bc454e4438f44e",
    "ETHERSCAN_API_KEY": "test-key-not-real",
    "TRONSCAN_API_KEY": "test-key-not-real",
}


def run(base: Path, body: str, **env: str) -> subprocess.CompletedProcess[str]:
    """Source the CLI, point it at a throwaway INSTALL_BASE, run ``body``.

    The overrides are assignments after the source rather than parameters of
    the script: production has no need for a configurable install path, and a
    knob added only so that tests can reach a function is a knob an operator
    will eventually find and use.
    """
    script = f"""
    set -uo pipefail
    source {SCRIPT}
    INSTALL_BASE={base}
    ENV_FILE="$INSTALL_BASE/.env"
    COMPOSE_DIR="$INSTALL_BASE/compose"
    ENV_LINK="$COMPOSE_DIR/.env"
    BACKUP_DIR="$INSTALL_BASE/backups"
    {body}
    """
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(base), **ANSWERS, **env},
        # A new session has no controlling terminal, so /dev/tty cannot be
        # opened. Without this, the two tests that assert an abort would sit
        # waiting for input on any machine where pytest is run from a terminal
        # -- passing in CI and hanging for a developer.
        start_new_session=True,
    )


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key] = value
    return values


@pytest.fixture(autouse=True)
def _needs_bash():
    if shutil.which("bash") is None or not SCRIPT.exists():  # pragma: no cover
        pytest.skip("bash or the deploy CLI is unavailable")


# --------------------------------------------------------------------------
# generate_env: minted once, asked once
# --------------------------------------------------------------------------


def test_a_fresh_install_mints_three_secrets_and_records_the_answers(tmp_path: Path):
    result = run(tmp_path, "generate_env")

    assert result.returncode == 0, result.stderr
    env = read_env(tmp_path / ".env")
    assert len(env["POSTGRES_PASSWORD"]) == 48
    assert len(env["SERVICE_TOKEN"]) == 64
    assert len(env["PAYMENTS_WEBHOOK_SECRET"]) == 64
    for key, value in ANSWERS.items():
        assert env[key] == value
    # The one value nobody knows yet, and the reason install runs twice.
    assert env["PRODUCT_WEBHOOK_URL"] == ""


def test_the_database_url_carries_the_minted_password(tmp_path: Path):
    """One password, two places, written from one variable.

    A DATABASE_URL that disagreed with POSTGRES_PASSWORD would produce a stack
    that starts and cannot reach its own database -- and only on the first
    query, not at startup.
    """
    run(tmp_path, "generate_env")

    env = read_env(tmp_path / ".env")
    assert env["POSTGRES_PASSWORD"] in env["DATABASE_URL"]


def test_a_second_install_re_mints_nothing_and_re_asks_nothing(tmp_path: Path):
    """The done-when of P-18, and the reason the guard is on the FILE.

    Re-minting the database password beside a surviving data volume locks the
    stack out of its own database; re-asking would let a second pass quietly
    replace a wallet address that is already snapshotted onto live invoices.
    """
    run(tmp_path, "generate_env")
    before = (tmp_path / ".env").read_text()

    result = run(
        tmp_path,
        "generate_env",
        WALLET_ADDRESS_USDT_TRC20="TMuA6YqfCeX8EhbfYEg5y7S4DqzSJireY9",
    )

    assert result.returncode == 0
    # Byte for byte: not merely "the secrets survived" but "nothing moved",
    # including the answer the second run was handed a different value for.
    assert (tmp_path / ".env").read_text() == before


def test_a_missing_answer_without_a_terminal_aborts_instead_of_guessing(
    tmp_path: Path,
):
    """No placeholders here, and this is where that decision is enforced.

    A generated stand-in for a wallet address is an address a human sends money
    to. So the install stops, names the variable, and leaves no file behind --
    the guard above means a half-written one would never be completed.
    """
    result = run(
        tmp_path,
        "generate_env",
        WALLET_ADDRESS_USDT_TRC20="",
    )

    assert result.returncode != 0
    assert "WALLET_ADDRESS_USDT_TRC20" in result.stderr
    assert not (tmp_path / ".env").exists()


def test_an_interrupted_install_leaves_no_partial_file(tmp_path: Path):
    """Atomicity, and why it is not tidiness.

    The guard is on the file's existence, so a file that appeared half-written
    would nail an empty wallet address in place with no path that ever fills
    it. Every value is collected first and the file is moved into place whole.
    """
    result = run(
        tmp_path,
        "generate_env",
        TRONSCAN_API_KEY="",
    )

    assert result.returncode != 0
    assert not (tmp_path / ".env").exists()
    assert not (tmp_path / ".env.tmp").exists()


def test_the_generated_file_is_not_world_readable(tmp_path: Path):
    """It holds three minted secrets and two API keys."""
    run(tmp_path, "generate_env")

    assert (tmp_path / ".env").stat().st_mode & 0o077 == 0


# --------------------------------------------------------------------------
# handover_token: write when it can, print when it cannot
# --------------------------------------------------------------------------


def test_the_hand_over_writes_all_three_variables_into_the_product_env(
    tmp_path: Path,
):
    product = tmp_path / "product.env"
    product.write_text("EXISTING=1\n")
    run(tmp_path, "generate_env")

    result = run(tmp_path, "handover_token", PRODUCT_ENV_PATH=str(product))

    assert result.returncode == 0
    written = read_env(product)
    ours = read_env(tmp_path / ".env")
    assert written["PAYMENTS_SERVICE_TOKEN"] == ours["SERVICE_TOKEN"]
    assert written["PAYMENTS_WEBHOOK_SECRET"] == ours["PAYMENTS_WEBHOOK_SECRET"]
    assert written["PAYMENTS_API_URL"] == "http://payments-app:8000"
    # Somebody else's file: what was already in it stays.
    assert written["EXISTING"] == "1"


def test_a_second_hand_over_updates_in_place_rather_than_appending(tmp_path: Path):
    """Idempotence on a file we do not own.

    An env file resolves a repeated key to the last assignment, so appending
    would still work -- and would grow the product's file by three lines on
    every install, until nobody can tell which value is live.
    """
    product = tmp_path / "product.env"
    product.write_text("PAYMENTS_API_URL=http://stale:1\n")
    run(tmp_path, "generate_env")

    run(tmp_path, "handover_token", PRODUCT_ENV_PATH=str(product))
    run(tmp_path, "handover_token", PRODUCT_ENV_PATH=str(product))

    lines = product.read_text().splitlines()
    assert sum(line.startswith("PAYMENTS_API_URL=") for line in lines) == 1
    assert read_env(product)["PAYMENTS_API_URL"] == "http://payments-app:8000"


def test_an_empty_product_path_prints_the_block_and_succeeds(tmp_path: Path):
    """The fallback that lets a second product integrate without asking us.

    Success, not failure: a manual flow is a supported flow. It is also why the
    orchestrating installer verifies the seam afterwards -- for orchestration
    this same success would be a silent failure.
    """
    run(tmp_path, "generate_env")

    result = run(tmp_path, "handover_token", PRODUCT_ENV_PATH="")

    assert result.returncode == 0
    ours = read_env(tmp_path / ".env")
    assert f"PAYMENTS_SERVICE_TOKEN={ours['SERVICE_TOKEN']}" in result.stdout
    assert f"PAYMENTS_WEBHOOK_SECRET={ours['PAYMENTS_WEBHOOK_SECRET']}" in result.stdout
    assert "PAYMENTS_API_URL=http://payments-app:8000" in result.stdout


def test_a_product_path_that_does_not_exist_prints_rather_than_creates(
    tmp_path: Path,
):
    """Creating somebody else's .env from scratch would be worse than not
    writing at all: the product would find a file holding three variables and
    nothing else it needs."""
    missing = tmp_path / "nope" / "product.env"
    run(tmp_path, "generate_env")

    result = run(tmp_path, "handover_token", PRODUCT_ENV_PATH=str(missing))

    assert result.returncode == 0
    assert not missing.exists()
    assert "PAYMENTS_SERVICE_TOKEN=" in result.stdout


# --------------------------------------------------------------------------
# env_is_complete: the one value that gates the bring-up
# --------------------------------------------------------------------------


def test_a_fresh_env_is_not_complete_and_a_delivered_url_completes_it(
    tmp_path: Path,
):
    """Why pass 1 stops without starting anything.

    PRODUCT_WEBHOOK_URL is mandatory with no default, so a container started
    without it exits on config validation. Refusing to start it is the same
    answer arrived at two minutes earlier and with a sentence instead of a
    crash loop.
    """
    run(tmp_path, "generate_env")

    fresh = run(tmp_path, "env_is_complete")
    assert fresh.returncode != 0

    run(
        tmp_path,
        'upsert_env_var "$ENV_FILE" PRODUCT_WEBHOOK_URL '
        "http://aivis-app:8000/api/v1/payments/webhook",
    )
    delivered = run(tmp_path, "env_is_complete")
    assert delivered.returncode == 0


def test_a_repeated_key_resolves_to_the_last_assignment(tmp_path: Path):
    """``read_env_value`` reads an env file the way a shell would.

    The hand-over from the product side upserts, so a duplicate should not
    arise -- but the file is edited by two scripts and by hand, and reading the
    first of two assignments would report a value that is not the live one.
    """
    env = tmp_path / ".env"
    env.write_text("PRODUCT_WEBHOOK_URL=http://first/\nPRODUCT_WEBHOOK_URL=http://second/\n")

    result = run(tmp_path, 'read_env_value "$ENV_FILE" PRODUCT_WEBHOOK_URL')

    assert result.stdout.strip() == "http://second/"


# --------------------------------------------------------------------------
# The verb list, which the product's CLI reads
# --------------------------------------------------------------------------


def test_the_script_offers_exactly_the_documented_verbs(tmp_path: Path):
    """``update`` in particular: the product's registry names it as this
    service's lifecycle verb, and a rename here would break every later
    update with no failure anywhere near this repository.
    """
    usage = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True
    ).stdout

    for verb in ("install", "update", "start", "stop", "restart", "logs", "db", "status"):
        assert verb in usage
    # comms has one; this image ships no tests and no dev dependencies, so the
    # same verb here could not run.
    assert "test" not in usage.split("{", 1)[1].split("}", 1)[0].split("|")
