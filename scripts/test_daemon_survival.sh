#!/usr/bin/env bash
# Layer 2 of the test plan, the scripted part: an installed daemon survives
#   1. a daemon restart   (launchctl kickstart -k: SIGTERM, drain, respawn),
#   2. a Postgres outage  (the team container stopped across a board check,
#                          then started: the daemon keeps running, buffers
#                          that check's event and writes it once Postgres is
#                          back),
#   3. a launchd reload   (bootout + bootstrap of its plist).
# The proof each time is a row the running daemon wrote to the team database
# after the step: the board check's job.skipped event (the schedule rule
# stops in preRun), which needs the daemon alive, the schedule firing and its
# database connection working. /api/health and the pid change (a new daemon
# after 1 and 3, the same one after 2) are supporting evidence.
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
CONTAINER=agency-team-pg
PSQL=(docker exec "$CONTAINER" psql -U agency_team_admin -d agency_team -tAc)
# One two-minute cron tick plus a restart.
WAIT_SECS=200

fail() { printf 'survival: FAIL %s\n' "$*" >&2; exit 1; }
pass() { printf 'survival: ok   %s\n' "$*"; }
job_pid() { launchctl print "$domain/$label" 2>/dev/null | sed -n 's/^[[:space:]]*pid = //p'; }
utc_now() { date -u '+%Y-%m-%d %H:%M:%S+00'; }

wait_healthy() {
  local deadline=$((SECONDS + 120))
  until curl -fsS "http://127.0.0.1:$port/api/health" >/dev/null 2>&1; do
    [ "$SECONDS" -lt "$deadline" ] || fail "$1: /api/health on $port did not answer within 120s"
    sleep 2
  done
  pass "$1: /api/health answers on $port"
}

wait_postgres() {
  local deadline=$((SECONDS + 60))
  until docker exec "$CONTAINER" pg_isready >/dev/null 2>&1; do
    [ "$SECONDS" -lt "$deadline" ] || fail "$1: $CONTAINER not ready within 60s"
    sleep 1
  done
}

# Board-check events this agent wrote with a timestamp in [$2, $3).
board_check_events() {
  "${PSQL[@]}" "SELECT count(*) FROM events WHERE agent_id = '$agent'
    AND route_id = 'schedule/board-check' AND ts >= '$1' AND ts < '$2'"
}

# A board-check event stamped at or after $2, written to the database.
wait_written() {
  local deadline=$((SECONDS + WAIT_SECS))
  until [ "$(board_check_events "$2" 'infinity')" -gt 0 ]; do
    [ "$SECONDS" -lt "$deadline" ] || fail "$1: no board-check event written since $2 within ${WAIT_SECS}s"
    sleep 5
  done
  pass "$1: board check fired and its event is in the database"
}

launchctl print "$domain/$label" >/dev/null 2>&1 || fail "$label is not loaded; run make install-daemon AGENT=$agent"
before=$(job_pid); [ -n "$before" ] || fail "$label has no running pid"
wait_healthy "baseline"

# 1. Daemon restart.
since=$(utc_now)
launchctl kickstart -k "$domain/$label"
wait_healthy "daemon restart"
after=$(job_pid)
[ -n "$after" ] && [ "$after" != "$before" ] || fail "daemon restart: pid still $before"
pass "daemon restart: pid $before -> $after"
wait_written "daemon restart" "$since"

# 2. Postgres outage across a board check.
before=$after
offset=$(wc -c <"$log")
down=$(utc_now)
docker stop "$CONTAINER" >/dev/null
pass "postgres outage: $CONTAINER stopped"
deadline=$((SECONDS + WAIT_SECS))
until tail -c "+$((offset + 1))" "$log" | grep -q '"schedule stopped in preRun'; do
  [ "$SECONDS" -lt "$deadline" ] || { docker start "$CONTAINER" >/dev/null; fail "postgres outage: no board check fired within ${WAIT_SECS}s"; }
  sleep 5
done
pass "postgres outage: a board check fired with the database down"
up=$(utc_now)
docker start "$CONTAINER" >/dev/null
wait_postgres "postgres outage"
pass "postgres outage: $CONTAINER is back"
wait_healthy "postgres outage"
[ "$(job_pid)" = "$before" ] || fail "postgres outage: the daemon restarted (pid $before -> $(job_pid))"
pass "postgres outage: daemon kept running (pid $before)"
# The outage's event is written when the next insert drains the buffer,
# stamped with the drain's time (Agency inserts buffered rows anew), so the
# proof is the drain in the log, an empty buffer and the rows since $up.
wait_written "postgres outage" "$up"
tail -c "+$((offset + 1))" "$log" | grep -q '"events: drained buffer rows after recovery"' \
  || fail "postgres outage: the daemon never drained its events buffer"
[ ! -s "$RUNTIME/$agent/events-buffer.jsonl" ] || fail "postgres outage: events left in the buffer"
[ "$(board_check_events "$down" 'infinity')" -ge 2 ] || fail "postgres outage: the buffered event is not in the database"
pass "postgres outage: the event buffered while down was written"

# 3. launchd reload.
since=$(utc_now)
launchctl bootout "$domain/$label"
launchctl bootstrap "$domain" "$plist"
wait_healthy "launchd reload"
after=$(job_pid)
[ -n "$after" ] && [ "$after" != "$before" ] || fail "launchd reload: pid still $before"
pass "launchd reload: pid $before -> $after"
wait_written "launchd reload" "$since"

echo "survival: all three passed for $agent"
