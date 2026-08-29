#!/bin/bash
# -u: abort on unset variables. pipefail: a pipeline fails if any stage fails.
# No -e: deliberately absent (inherited discipline from comms-deploy.sh,
# verified there): `set -e` is suspended inside if/&&/|| contexts, so every
# command whose failure matters is checked EXPLICITLY instead, right where it
# runs.
set -uo pipefail

# ==============================================================================
# PAYMENTS Deploy CLI -- sibling of comms-deploy.sh
# ==============================================================================
#
# Repeatable bring-up and lifecycle of the payments stack on a product VPS,
# NEXT TO the product stack: dedicated containers on the shared external
# network "aivis-shared". Product-agnostic by design: no product vocabulary in
# here, and in particular no product path and no product URL -- those are
# VALUES delivered into the env file by whoever runs the product.
#
# TRACKED in the repo, next to the compose it drives: a provisioned-once copy
# drifts, while `update` pulls this file like any other, so a fix here reaches
# every server on the next update.
#
# Layout on the VPS (mirrors the product's install):
#   /opt/payments/                INSTALL_BASE -- per-instance state
#   /opt/payments/.env            master env (secrets; written ONCE)
#   /opt/payments/backups/        db dumps
#   /opt/payments/repo/           the payments checkout
#   /opt/payments/repo/deploy/    compose + this script
#   /opt/payments/repo/deploy/.env -> /opt/payments/.env  (symlink; compose
#                                 reads ./.env for env_file)
#
# Subcommands: install | update | start | stop | restart | logs | db | status.
#
# THREE LIFECYCLE VERBS, THREE DIFFERENT WIDTHS -- the difference is deliberate
# and easy to erase by "unifying" them later:
#   restart  bounces the TWO app containers only, and waits for health. Narrow
#            because the datastore holds state and bouncing it for an
#            application-level change is gratuitous risk.
#   stop     takes the WHOLE stack down, postgres included. It is the switch
#            thrown when the machine goes off, not an application-level
#            operation.
#   start    brings the whole stack back up and waits for health.
#
# NO `test` SUBCOMMAND, and its absence is a decision. comms has one because
# its image is built for it -- dev dependencies installed, tests copied in.
# This image installs the project without its dev extra and ships no tests, so
# the same verb here would be a command that cannot run. The suite has two
# owners already: CI on every push, and the developer's machine.
#
# WHAT DIFFERS FROM comms-deploy.sh, and why -- both differences are forced by
# the subject rather than chosen:
#
#   1. `install` ASKS for five values (three wallet addresses, two explorer
#      keys). comms mints everything it needs; we cannot invent an address a
#      human will send money to. An empty answer is re-asked, never kept.
#
#   2. `install` brings the stack up only when the env is COMPLETE.
#      PRODUCT_WEBHOOK_URL is mandatory and has no default -- the service
#      refuses to boot without it, by design -- and on the first pass nobody
#      knows it yet. comms has no equivalent: its own knob is read by the shell
#      only, and its bot token gets a placeholder. So pass 1 mints, asks and
#      stops with a clear statement of what is missing; pass 2, after the value
#      has been delivered, brings the stack up.
#
# Usage: payments-deploy.sh {install|update|start|stop|restart|logs|db|status}
# ==============================================================================

INSTALL_BASE="/opt/payments"
REPO_DIR="$INSTALL_BASE/repo"
COMPOSE_DIR="$REPO_DIR/deploy"
ENV_FILE="$INSTALL_BASE/.env"
ENV_LINK="$COMPOSE_DIR/.env"
BACKUP_DIR="$INSTALL_BASE/backups"
NETWORK_NAME="aivis-shared"
COMPOSE_CMD="docker compose"
APP_PORT=8000
APP_CONTAINER="payments-app"
WORKER_CONTAINER="payments-worker"
DB_CONTAINER="payments-postgres"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# Ensure we're in the right directory for docker compose.
cd_compose() {
    cd "$COMPOSE_DIR" || {
        echo -e "${RED}ERROR: $COMPOSE_DIR not found -- is the payments repo cloned to $REPO_DIR?${NC}"
        exit 1
    }
}

# Source the master env file (simple KEY=VALUE lines -- openssl hex, addresses,
# API keys and one URL, all safe to source).
load_env() {
    if [ ! -f "$ENV_FILE" ]; then
        echo -e "${RED}ERROR: $ENV_FILE not found -- run 'install' first.${NC}"
        exit 1
    fi
    # shellcheck source=/dev/null
    source "$ENV_FILE"
}

# Read one KEY's value out of an env file. `grep`, deliberately not `source`:
# used on files this script does not own.
#
# `tail -n 1`, not `grep -m1`: an env file resolves a repeated key to the LAST
# assignment, exactly as a shell would.
read_env_value() {
    local file="$1" key="$2"
    [ -f "$file" ] || return 1
    grep -E "^${key}=" "$file" 2>/dev/null | tail -n 1 | cut -d= -f2-
}

# Idempotent KEY=VALUE write into an env file: update in place when the key
# exists, append when it does not. Values here carry no '|', which is the sed
# delimiter.
upsert_env_var() {
    local file="$1" key="$2" value="$3"
    if grep -q "^${key}=" "$file"; then
        if ! sed -i "s|^${key}=.*|${key}=${value}|" "$file"; then
            echo -e "${RED}✗ Failed to update ${key} in ${file}${NC}"
            return 1
        fi
    else
        if ! printf '%s=%s\n' "$key" "$value" >> "$file"; then
            echo -e "${RED}✗ Failed to append ${key} to ${file}${NC}"
            return 1
        fi
    fi
    return 0
}

# ------------------------------------------------------------------------------
# install steps -- each one IDEMPOTENT on its own, so a re-run after a partial
# failure resumes instead of wrecking existing state.
# ------------------------------------------------------------------------------

# Step 1: the shared external network. No-op if it already exists (the
# product's installer may have created it first -- either side may win the
# race, the result is identical).
ensure_network() {
    if docker network inspect "$NETWORK_NAME" > /dev/null 2>&1; then
        echo -e "${GREEN}✓ Network '$NETWORK_NAME' already exists${NC}"
    else
        if docker network create "$NETWORK_NAME" > /dev/null; then
            echo -e "${GREEN}✓ Network '$NETWORK_NAME' created${NC}"
        else
            echo -e "${RED}✗ Failed to create network '$NETWORK_NAME'${NC}"
            exit 1
        fi
    fi
}

# Ask the operator for one value that no script can invent.
#
# SAME SHAPE as the product installer's prompt_secret -- label, minimum length,
# re-ask on a short answer -- and one deliberate difference: ENTER does NOT
# mean "keep the current value". There it keeps a PLACEHOLDER in an optional
# field; here the current value is empty, and keeping it would put an address
# nobody owns onto every invoice this service issues.
#
# Reads from /dev/tty rather than stdin, so a redirected stdin does not silently
# turn a question into an empty answer -- the same choice the product installer
# makes, and two scripts on one machine should behave alike.
#
# `printf %s` on the prompt rather than `read -p`: the value is echoed as it is
# typed (the operator wants to see the address they are pasting), and the
# terminal advances the line itself on Enter.
ask_value() {
    local label="$1" min_len="$2" value=""

    while true; do
        printf '  %s: ' "$label" > /dev/tty
        if ! read -r value < /dev/tty; then
            echo -e "${RED}✗ No terminal available to ask for '$label'${NC}"
            return 1
        fi
        if [ -z "$value" ]; then
            echo -e "${YELLOW}  $label: required -- an empty value here is a payment nobody receives${NC}" > /dev/tty
            continue
        fi
        if [ "$min_len" -gt 0 ] && [ "${#value}" -lt "$min_len" ]; then
            echo -e "${YELLOW}  $label: must be at least $min_len characters (got ${#value})${NC}" > /dev/tty
            continue
        fi
        printf '%s' "$value"
        return 0
    done
}

# Resolve one ASKED value: the process environment first, the operator second.
#
# The environment path is what makes a scripted install possible at all. It is
# NOT a fallback to a default: a value that is neither in the environment nor
# answerable at a terminal aborts the install, because the alternative is a
# placeholder in a wallet address.
resolve_value() {
    local var="$1" label="$2" min_len="${3:-0}" preset="${!1:-}"

    if [ -n "$preset" ]; then
        echo -e "${GREEN}✓ $label taken from the environment${NC}" > /dev/tty 2>/dev/null || true
        printf '%s' "$preset"
        return 0
    fi
    if [ ! -e /dev/tty ] || ! ask_value "$label" "$min_len"; then
        echo -e "${RED}✗ $var is not set and cannot be asked for (no terminal).${NC}" >&2
        echo -e "${RED}  Set it in the environment before running install:${NC}" >&2
        echo -e "${RED}    $var=<value> $0 install${NC}" >&2
        return 1
    fi
}

# Step 2: the master env with secrets. THE GUARD: an existing file is NEVER
# regenerated -- secrets are minted exactly once, and the answers already given
# are never asked for again.
#
# WRITTEN ATOMICALLY, and that is not tidiness. The guard above means the file
# is written once and completed never; a file that appeared half-filled --
# because the operator interrupted the questions -- would nail an empty wallet
# address in place permanently, with no path that ever fills it. So every value
# is collected first, the file is built beside its destination, and only a
# successful build is moved into place.
generate_env() {
    if [ -f "$ENV_FILE" ]; then
        echo -e "${GREEN}✓ $ENV_FILE already exists -- secrets NOT re-minted, nothing re-asked${NC}"
        return 0
    fi

    local pg_pass service_token webhook_secret
    pg_pass=$(openssl rand -hex 24) || { echo -e "${RED}✗ openssl failed${NC}"; exit 1; }
    service_token=$(openssl rand -hex 32) || { echo -e "${RED}✗ openssl failed${NC}"; exit 1; }
    webhook_secret=$(openssl rand -hex 32) || { echo -e "${RED}✗ openssl failed${NC}"; exit 1; }

    echo ""
    echo -e "${CYAN}Five values this installer cannot invent.${NC}"
    echo -e "${YELLOW}Wallet addresses are snapshotted onto every invoice, so a wrong one is${NC}"
    echo -e "${YELLOW}copied into the database and survives any later fix to this file.${NC}"
    echo ""

    local trc20 erc20 bsc20 etherscan_key tronscan_key
    trc20=$(resolve_value WALLET_ADDRESS_USDT_TRC20 "USDT-TRC20 wallet address (TRON)" 34) || exit 1
    erc20=$(resolve_value WALLET_ADDRESS_USDT_ERC20 "USDT-ERC20 wallet address (Ethereum)" 42) || exit 1
    bsc20=$(resolve_value WALLET_ADDRESS_USDT_BSC20 "USDT-BSC20 wallet address (BSC)" 42) || exit 1
    etherscan_key=$(resolve_value ETHERSCAN_API_KEY "Etherscan V2 API key (covers ERC20 and BSC20)" 8) || exit 1
    tronscan_key=$(resolve_value TRONSCAN_API_KEY "TronScan API key" 8) || exit 1

    mkdir -p "$INSTALL_BASE"
    local tmp="$ENV_FILE.tmp"
    rm -f "$tmp"
    touch "$tmp" && chmod 600 "$tmp"
    cat > "$tmp" <<EOF
# PAYMENTS deploy env -- GENERATED by payments-deploy.sh install $(date -u +%Y-%m-%dT%H:%M:%SZ)
# Secrets are minted ONCE; this file is never regenerated while it exists.
# Reference for every variable: deploy/.env.example.

APP_ENV=production
LOG_LEVEL=INFO

POSTGRES_USER=payments
POSTGRES_DB=payments
POSTGRES_PASSWORD=$pg_pass
DATABASE_URL=postgresql+asyncpg://payments:$pg_pass@$DB_CONTAINER:5432/payments

SERVICE_TOKEN=$service_token
PAYMENTS_WEBHOOK_SECRET=$webhook_secret

WALLET_ADDRESS_USDT_TRC20=$trc20
WALLET_ADDRESS_USDT_ERC20=$erc20
WALLET_ADDRESS_USDT_BSC20=$bsc20

ETHERSCAN_API_KEY=$etherscan_key
TRONSCAN_API_KEY=$tronscan_key

# Delivered by the PRODUCT's installer -- the address of its webhook receiver
# on $NETWORK_NAME. Empty means the stack is not brought up yet: the service
# is mandatory-fail-closed on this value and would refuse to boot.
PRODUCT_WEBHOOK_URL=
EOF
    if ! mv "$tmp" "$ENV_FILE"; then
        rm -f "$tmp"
        echo -e "${RED}✗ Failed to write $ENV_FILE${NC}"
        exit 1
    fi
    echo -e "${GREEN}✓ $ENV_FILE generated (postgres password, service token and webhook secret minted)${NC}"
}

# Step 3: compose reads ./.env next to docker-compose.yml -- link it to the
# master outside the checkout, so `update` (git) never touches secrets.
# ln -sfn is idempotent.
ensure_env_link() {
    if ln -sfn "$ENV_FILE" "$ENV_LINK"; then
        echo -e "${GREEN}✓ $ENV_LINK -> $ENV_FILE${NC}"
    else
        echo -e "${RED}✗ Failed to link $ENV_LINK${NC}"
        exit 1
    fi
}

# Step 4: the trust seam. The three PAYMENTS_* variables the product backend
# needs, delivered from the SINGLE source (our env). The target is pure
# CONFIG: set -> written straight into the product's .env, idempotently; empty
# -> the block is printed for manual paste. No product path lives in this code.
#
# PAYMENTS_API_URL and PAYMENTS_SERVICE_TOKEN go together or not at all -- the
# product's own config refuses to start with one without the other, and a
# half-written seam would be a product that does not come up.
# SERVICE_TOKEN and PAYMENTS_WEBHOOK_SECRET come from `source "$ENV_FILE"` in
# load_env, which shellcheck cannot see through -- it reads the lowercase
# locals in generate_env and suspects a typo.
# shellcheck disable=SC2153
handover_token() {
    load_env
    local api_url="http://${APP_CONTAINER}:${APP_PORT}"
    local target="${PRODUCT_ENV_PATH:-}"

    if [ -n "$target" ] && [ -f "$target" ]; then
        local ok=0
        upsert_env_var "$target" "PAYMENTS_SERVICE_TOKEN" "$SERVICE_TOKEN" || ok=1
        upsert_env_var "$target" "PAYMENTS_API_URL" "$api_url" || ok=1
        upsert_env_var "$target" "PAYMENTS_WEBHOOK_SECRET" "$PAYMENTS_WEBHOOK_SECRET" || ok=1
        if [ "$ok" -eq 0 ]; then
            echo -e "${GREEN}✓ PAYMENTS_SERVICE_TOKEN / PAYMENTS_API_URL / PAYMENTS_WEBHOOK_SECRET written into $target${NC}"
            echo -e "${YELLOW}  Restart the product backend to pick them up${NC}"
            return 0
        fi
        echo -e "${RED}✗ Could not write all variables into $target -- paste the block below manually${NC}"
    elif [ -n "$target" ]; then
        echo -e "${YELLOW}PRODUCT_ENV_PATH is set but '$target' does not exist -- paste this block into the product's .env manually:${NC}"
    else
        echo -e "${YELLOW}PRODUCT_ENV_PATH is empty (see deploy/INTEGRATION.md for the product's value) -- paste this block into the product's .env manually:${NC}"
    fi
    echo
    echo "PAYMENTS_SERVICE_TOKEN=$SERVICE_TOKEN"
    echo "PAYMENTS_API_URL=$api_url"
    echo "PAYMENTS_WEBHOOK_SECRET=$PAYMENTS_WEBHOOK_SECRET"
    echo
}

# Is the env complete enough for the service to boot?
#
# One value decides it. PRODUCT_WEBHOOK_URL is mandatory with no default, so a
# container started without it exits on config validation -- which is a correct
# refusal, and a confusing way to discover that the second pass has not run.
env_is_complete() {
    local url
    url=$(read_env_value "$ENV_FILE" "PRODUCT_WEBHOOK_URL" || true)
    [ -n "$url" ]
}

# Poll the app container's health until healthy, or stop early when it is
# clear no amount of waiting will help.
#
# TWO DIFFERENCES FROM THE DONOR, both about the minutes an operator spends
# staring at a script:
#
#   * A container that has EXITED is not waited for. The service validates its
#     config at import and dies in about a second on a bad wallet address;
#     counting sixty checks at two seconds each afterwards tells nobody
#     anything, and a silent script is indistinguishable from a hung one.
#   * The tail of the container log is printed on failure. The donor points at
#     `logs`, which is right when the cause is unknown -- here one whole class
#     of failure has a known, one-line cause sitting in that log, and making
#     the operator go and fetch it by hand is a step with no purpose.
wait_for_app() {
    local attempts=60 status state
    echo "Waiting for $APP_CONTAINER to become healthy (migration runs first)..."
    for i in $(seq 1 "$attempts"); do
        status=$(docker inspect --format '{{.State.Health.Status}}' "$APP_CONTAINER" 2>/dev/null)
        if [ "$status" = "healthy" ]; then
            echo -e "${GREEN}✓ $APP_CONTAINER is healthy (after ${i} checks)${NC}"
            return 0
        fi
        state=$(docker inspect --format '{{.State.Status}}' "$APP_CONTAINER" 2>/dev/null)
        if [ "$state" = "exited" ] || [ "$state" = "dead" ]; then
            echo -e "${RED}✗ $APP_CONTAINER exited instead of starting${NC}"
            report_app_failure
            return 1
        fi
        sleep 2
    done
    echo -e "${RED}✗ $APP_CONTAINER did not become healthy${NC}"
    report_app_failure
    return 1
}

# The last lines of the app's own log, which is where the reason lives.
report_app_failure() {
    echo -e "${YELLOW}-- last 20 lines of $APP_CONTAINER --${NC}"
    docker logs --tail 20 "$APP_CONTAINER" 2>&1 || true
    echo -e "${YELLOW}-- end --${NC}"
    echo -e "${YELLOW}A rejected wallet address or API key appears here as a config error.${NC}"
    echo -e "${YELLOW}It cannot be fixed by re-running install: $ENV_FILE is written once.${NC}"
    echo -e "${YELLOW}Correct it there and run '$0 start', or wipe $INSTALL_BASE and install again.${NC}"
    echo "Full log: $0 logs $APP_CONTAINER"
}

# ------------------------------------------------------------------------------
# Subcommands
# ------------------------------------------------------------------------------

cmd_install() {
    echo -e "${CYAN}== payments install ==${NC}"
    cd_compose
    ensure_network
    generate_env
    ensure_env_link
    handover_token

    if ! env_is_complete; then
        echo ""
        echo -e "${YELLOW}PRODUCT_WEBHOOK_URL is empty in $ENV_FILE -- the stack was NOT started.${NC}"
        echo -e "${YELLOW}The service refuses to boot without the product's webhook address, so${NC}"
        echo -e "${YELLOW}starting it now would only produce a container that exits.${NC}"
        echo -e "${YELLOW}Write the value into $ENV_FILE and run install again (pass 2); the${NC}"
        echo -e "${YELLOW}product's installer does this between its two passes.${NC}"
        return 0
    fi

    echo "Building and starting the payments stack..."
    if ! $COMPOSE_CMD up -d --build; then
        echo -e "${RED}✗ compose up failed${NC}"
        exit 1
    fi
    if ! wait_for_app; then
        exit 1
    fi
    echo -e "${GREEN}✓ payments stack is up on '$NETWORK_NAME'${NC}"
}

cmd_update() {
    echo -e "${CYAN}== payments update ==${NC}"
    cd "$REPO_DIR" || {
        echo -e "${RED}ERROR: $REPO_DIR not found${NC}"
        exit 1
    }
    # Explicit check: a failed pull must not silently rebuild stale code as
    # "updated".
    if ! git pull --ff-only; then
        echo -e "${RED}✗ git pull failed -- update aborted, nothing rebuilt${NC}"
        exit 1
    fi
    cd_compose
    ensure_env_link
    if ! $COMPOSE_CMD build; then
        echo -e "${RED}✗ image build failed -- containers left as they were${NC}"
        exit 1
    fi
    # A recreated payments-app re-runs `alembic upgrade head` in its command
    # before serving -- the migration IS the restart path.
    if ! $COMPOSE_CMD up -d; then
        echo -e "${RED}✗ compose up failed${NC}"
        exit 1
    fi
    if ! wait_for_app; then
        exit 1
    fi
    echo -e "${GREEN}✓ payments updated (pulled, rebuilt, migrated)${NC}"
}

# Restart the two application containers and wait for the API to be healthy
# again. postgres is left alone on purpose: it holds the data, and bouncing it
# for an application-level change is gratuitous risk.
cmd_restart() {
    echo -e "${CYAN}== payments restart ==${NC}"
    cd_compose
    if ! $COMPOSE_CMD restart "$APP_CONTAINER" "$WORKER_CONTAINER"; then
        echo -e "${RED}✗ restart failed${NC}"
        exit 1
    fi
    # `compose restart` returns as soon as it has signalled the containers --
    # it says nothing about what happened next.
    if ! wait_for_app; then
        exit 1
    fi
    echo -e "${GREEN}✓ $APP_CONTAINER / $WORKER_CONTAINER restarted${NC}"
}

# Bring the whole stack up and wait until it is actually serving.
#
# ORDER IS NOT WRITTEN HERE ON PURPOSE. deploy/docker-compose.yml already
# declares it, so `up -d` starts things in dependency order by itself.
# Spelling the sequence out again here would be a second copy of that graph.
#
# Idempotent: `up -d` on an already-running stack is a no-op.
cmd_start() {
    echo -e "${CYAN}== payments start ==${NC}"
    load_env
    cd_compose
    # The shared network is EXTERNAL: compose refuses to start if nobody has
    # created it, and on a machine that came up from cold the product's
    # installer is not necessarily the one that ran first.
    ensure_network
    if ! env_is_complete; then
        echo -e "${RED}✗ PRODUCT_WEBHOOK_URL is empty in $ENV_FILE -- the service would exit on startup.${NC}"
        exit 1
    fi
    if ! $COMPOSE_CMD up -d; then
        echo -e "${RED}✗ start failed${NC}"
        exit 1
    fi
    if ! wait_for_app; then
        exit 1
    fi
    echo -e "${GREEN}✓ payments stack is up${NC}"
}

# Take the WHOLE stack down -- app, worker AND the database. Wider than restart
# by design: this is called when a box is being shut down, and leaving a
# database running behind a `stop` would be a lie the operator only discovers
# in `docker ps`.
#
# Data survives: `down` removes containers, not named volumes. The shared
# external network is not ours to remove and compose leaves it alone.
cmd_stop() {
    echo -e "${CYAN}== payments stop ==${NC}"
    cd_compose
    if ! $COMPOSE_CMD down; then
        echo -e "${RED}✗ stop failed${NC}"
        exit 1
    fi
    echo -e "${GREEN}✓ payments stack is down (volumes kept)${NC}"
}

cmd_logs() {
    cd_compose
    $COMPOSE_CMD logs -f --tail=200 "$@"
}

cmd_db() {
    cd_compose
    load_env
    local action="${1:-}"
    case "$action" in
        dump)
            mkdir -p "$BACKUP_DIR"
            local out
            out="$BACKUP_DIR/payments-$(date -u +%Y%m%d-%H%M%S).sql"
            if $COMPOSE_CMD exec -T "$DB_CONTAINER" pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" > "$out"; then
                echo -e "${GREEN}✓ Dumped to $out${NC}"
            else
                rm -f "$out"
                echo -e "${RED}✗ pg_dump failed${NC}"
                exit 1
            fi
            ;;
        restore)
            local src="${2:-}"
            if [ -z "$src" ] || [ ! -f "$src" ]; then
                echo -e "${RED}Usage: $0 db restore <dump.sql>${NC}"
                exit 1
            fi
            echo -e "${YELLOW}This OVERWRITES the payments database from $src.${NC}"
            read -r -p "Type 'yes' to proceed: " answer
            if [ "$answer" != "yes" ]; then
                echo "Aborted."
                exit 1
            fi
            if $COMPOSE_CMD exec -T "$DB_CONTAINER" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" < "$src"; then
                echo -e "${GREEN}✓ Restored from $src${NC}"
            else
                echo -e "${RED}✗ restore failed${NC}"
                exit 1
            fi
            ;;
        migrate)
            # Manual migration outside the restart path (the normal one runs
            # inside payments-app's command on every start).
            if $COMPOSE_CMD exec "$APP_CONTAINER" alembic upgrade head; then
                echo -e "${GREEN}✓ Migrations applied${NC}"
            else
                echo -e "${RED}✗ alembic failed${NC}"
                exit 1
            fi
            ;;
        *)
            echo "Usage: $0 db {dump|restore <file>|migrate}"
            exit 1
            ;;
    esac
}

cmd_status() {
    cd_compose
    $COMPOSE_CMD ps
}

# ------------------------------------------------------------------------------
# Dispatch
# ------------------------------------------------------------------------------

# A driving product CLI discovers what this service implements by reading the
# labels of THIS case -- the one at column zero. Labels of the nested case
# inside cmd_db are arguments to `db`, not verbs of the service. Keep new
# lifecycle verbs here: a verb added anywhere else is invisible to the product.
#
# The guard around it makes the file SOURCEABLE: without it, sourcing this
# script to reach one function also runs the dispatch, which prints usage and
# exits the caller's shell. The half of this script that can be checked without
# a docker daemon -- that secrets are minted once, that the hand-over writes or
# prints -- is reachable only by sourcing it, so it is checked by
# tests/test_deploy_cli.py rather than by reading.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    case "${1:-}" in
        install) shift; cmd_install "$@" ;;
        update)  shift; cmd_update "$@" ;;
        start)   shift; cmd_start "$@" ;;
        stop)    shift; cmd_stop "$@" ;;
        restart) shift; cmd_restart "$@" ;;
        logs)    shift; cmd_logs "$@" ;;
        db)      shift; cmd_db "$@" ;;
        status)  shift; cmd_status "$@" ;;
        *)
            echo "Usage: $0 {install|update|start|stop|restart|logs [service]|db {dump|restore <file>|migrate}|status}"
            exit 1
            ;;
    esac
fi
