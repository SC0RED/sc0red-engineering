#!/usr/bin/env bash
# Start one agent's agency daemon: the launchd job's command.
#
#   start_daemon.sh <agent>
#
# Loads the installed workspace's agent.env, puts the 1Password service
# account's token (the login-keychain item agent.env names in
# OP_SERVICE_ACCOUNT_KEYCHAIN_ITEM) in the environment for Agency's
# SecretManager, and execs the installed agency binary through
# run_on_release.sh, so the job's pid is the daemon's. `make install-daemon`
# installs this beside run_on_release.sh; launchd runs that copy.
#
# The login keychain is locked from a reboot until the owner logs in; until
# then this exits 1 and launchd retries it every ThrottleInterval.
set -euo pipefail

RUNTIME=${AGENCY_RUNTIME_DIR:-/Volumes/SSD/agency-runtime}
fail() { printf 'start-daemon: %s\n' "$*" >&2; exit 1; }

[ "$#" -eq 1 ] || fail "usage: start_daemon.sh <agent>"
agent=$1
env_file="$RUNTIME/workspaces/$agent/agent.env"
[ -f "$env_file" ] || fail "$env_file is missing; run make install-daemon AGENT=$agent"

set -a
# shellcheck source=/dev/null
. "$env_file"
set +a

OP_SERVICE_ACCOUNT_TOKEN=$(security find-generic-password -s "$OP_SERVICE_ACCOUNT_KEYCHAIN_ITEM" -w 2>/dev/null) \
  || fail "no login-keychain item $OP_SERVICE_ACCOUNT_KEYCHAIN_ITEM (keychain locked until login, or item missing)"
export OP_SERVICE_ACCOUNT_TOKEN

printf 'start-daemon: %s starting %s on port %s at %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$agent" "$PORT" "$RUNTIME/agency/agency"
exec "$RUNTIME/bin/run_on_release.sh" "$RUNTIME/agency/agency"
