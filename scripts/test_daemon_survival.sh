#!/usr/bin/env bash
# Layer 2 of the test plan, the scripted part: an installed daemon survives
#   1. a daemon restart  (launchctl kickstart -k: SIGTERM, drain, respawn),
#   2. a Docker restart  (the team Postgres restarts under a running daemon),
#   3. a launchd reload  (bootout + bootstrap of its plist).
# After each, the daemon must answer /api/health and the board check must fire
# again (the schedule rule's preRun sweep, every two minutes), with the
# expected pid change: a new daemon after 1 and 3, the same one after 2.
#
#   test_daemon_survival.sh <agent>
#
# Reboot, a sleep attempt and a locked 1Password app are the owner's manual
# steps (README). Exits 1 at the first failed check.
set -euo pipefail

RUNTIME=${AGENCY_RUNTIME_DIR:-/Volumes/SSD/agency-runtime}
agent=${1:?usage: test_daemon_survival.sh <agent>}
label="com.sc0red.agency-$agent"
domain="gui/$(id -u)"
plist="$HOME/Library/LaunchAgents/$label.plist"
log="$RUNTIME/$agent/agency.log"
port=$(sed -n 's/^PORT=//p' "$RUNTIME/workspaces/$agent/agent.env")
# A board-check fire, as the daemon logs it: the preRun sweep step answered
# (whatever the sweep found, a refusal included).
FIRE_PATTERN='"route":"schedule/board-check/preRun:sweep'
# Long enough for a drain-free restart plus one two-minute cron tick.
FIRE_WAIT_SECS=200

fail() { printf 'survival: FAIL %s\n' "$*" >&2; exit 1; }
pass() { printf 'survival: ok   %s\n' "$*"; }
job_pid() { launchctl print "$domain/$label" 2>/dev/null | sed -n 's/^[[:space:]]*pid = //p'; }

wait_healthy() {
  local deadline=$((SECONDS + 120))
  until curl -fsS "http://127.0.0.1:$port/api/health" >/dev/null 2>&1; do
    [ "$SECONDS" -lt "$deadline" ] || fail "$1: /api/health on $port did not answer within 120s"
    sleep 2
  done
  pass "$1: /api/health answers on $port"
}

# A board-check fire logged after byte offset $2 of the log.
wait_fire() {
  local deadline=$((SECONDS + FIRE_WAIT_SECS))
  until tail -c "+$(($2 + 1))" "$log" | grep -q "$FIRE_PATTERN"; do
    [ "$SECONDS" -lt "$deadline" ] || fail "$1: no board-check fire in $log within ${FIRE_WAIT_SECS}s"
    sleep 5
  done
  pass "$1: board check fired"
}

launchctl print "$domain/$label" >/dev/null 2>&1 || fail "$label is not loaded; run make install-daemon AGENT=$agent"
before=$(job_pid); [ -n "$before" ] || fail "$label has no running pid"
wait_healthy "baseline"

# 1. Daemon restart.
offset=$(wc -c <"$log")
launchctl kickstart -k "$domain/$label"
wait_healthy "daemon restart"
after=$(job_pid)
[ -n "$after" ] && [ "$after" != "$before" ] || fail "daemon restart: pid still $before"
pass "daemon restart: pid $before -> $after"
wait_fire "daemon restart" "$offset"

# 2. Docker restart of the team Postgres under the running daemon.
before=$after
offset=$(wc -c <"$log")
docker restart agency-team-pg >/dev/null
until docker exec agency-team-pg pg_isready >/dev/null 2>&1; do sleep 1; done
pass "docker restart: agency-team-pg is back"
wait_healthy "docker restart"
[ "$(job_pid)" = "$before" ] || fail "docker restart: the daemon restarted (pid $before -> $(job_pid))"
pass "docker restart: daemon kept running (pid $before)"
wait_fire "docker restart" "$offset"

# 3. launchd reload.
offset=$(wc -c <"$log")
launchctl bootout "$domain/$label"
launchctl bootstrap "$domain" "$plist"
wait_healthy "launchd reload"
after=$(job_pid)
[ -n "$after" ] && [ "$after" != "$before" ] || fail "launchd reload: pid still $before"
pass "launchd reload: pid $before -> $after"
wait_fire "launchd reload" "$offset"

echo "survival: all three passed for $agent"
