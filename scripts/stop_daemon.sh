#!/usr/bin/env bash
# Stop a launchd job and wait until launchd has let it go.
#
#   stop_daemon.sh <domain/label>
#
# `launchctl bootout` returns while the job is still registered: the daemon
# drains its runs in flight first, which can take minutes. A `bootstrap` of
# the same label before then fails ("Bootstrap failed: 5: Input/output
# error"). So this waits until `launchctl print` no longer finds the job, up
# to STOP_DAEMON_TIMEOUT_SECS (default 30 minutes, since a run can be long),
# polling every STOP_DAEMON_POLL_SECS (default 2).
#
# Exit 0: the job is gone (or was never loaded). Exit 1: bootout failed, or
# the job was still registered at the timeout. The caller changes nothing
# after a non-zero exit.
set -uo pipefail

service=${1:?usage: stop_daemon.sh <domain/label>}
timeout=${STOP_DAEMON_TIMEOUT_SECS:-1800}
interval=${STOP_DAEMON_POLL_SECS:-2}

loaded() { launchctl print "$service" >/dev/null 2>&1; }

loaded || exit 0
echo "stop_daemon: stopping $service (waits for runs in flight, up to ${timeout}s)"
launchctl bootout "$service" || { echo "stop_daemon: launchctl bootout $service failed" >&2; exit 1; }
deadline=$((SECONDS + timeout))
while loaded; do
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "stop_daemon: $service is still registered ${timeout}s after bootout, so it is still finishing a run." \
      "Nothing else was changed. Once \`launchctl print $service\` fails, run this again." >&2
    exit 1
  fi
  sleep "$interval"
done
echo "stop_daemon: $service stopped"
