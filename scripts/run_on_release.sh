#!/usr/bin/env bash
# Start a daemon on one agency-tools release for its whole life.
#
#   scripts/run_on_release.sh <command> [args...]
#
# The daemon's launchd job loads agent.env, then runs its command through this.
# agent.env names AGENCY_TOOLS_ROOT, AGENCY_PYTHON_BINARY and
# AGENCY_CONTRACTS_DIR under $AGENCY_RUNTIME_DIR/current. This resolves
# `current` once, at spawn, to the real release directory, rewrites each of
# those three to the path inside that release, and execs the command, so the
# pid is the daemon's. Every tool process the daemon spawns inherits the real
# paths: a sync that swaps `current` later changes nothing under it. A restart
# picks up the new release.
#
# Releases are immutable and kept while current or previous, and for 7 days
# after they were last either (scripts/sync_agency_tools.sh), so restart a
# daemon within a week of its release leaving current and previous.
set -euo pipefail

RUNTIME=${AGENCY_RUNTIME_DIR:-/Volumes/SSD/agency-runtime}
current="$RUNTIME/current"

fail() { printf 'run-on-release: %s\n' "$*" >&2; exit 1; }

[ "$#" -gt 0 ] || fail "usage: run_on_release.sh <command> [args...]"
link=$(readlink "$current" 2>/dev/null) || fail "$current is not a release link; run make sync-tools"
release=$(cd "$RUNTIME" && cd "$link" && pwd -P) || fail "$current points at $link, which does not exist"
[ -f "$release/.complete" ] || fail "release $link is not complete"

for key in AGENCY_TOOLS_ROOT AGENCY_PYTHON_BINARY AGENCY_CONTRACTS_DIR; do
  value=${!key:-}
  case "$value" in
    "$current"/*) export "$key=$release/${value#"$current"/}" ;;
    *) fail "$key is '$value', not a path under $current (agent.env)" ;;
  esac
done
[ -d "$AGENCY_TOOLS_ROOT" ] || fail "release $link has no $AGENCY_TOOLS_ROOT"
[ -x "$AGENCY_PYTHON_BINARY" ] || fail "release $link has no $AGENCY_PYTHON_BINARY"
[ -d "$AGENCY_CONTRACTS_DIR" ] || fail "release $link has no $AGENCY_CONTRACTS_DIR"

exec "$@"
