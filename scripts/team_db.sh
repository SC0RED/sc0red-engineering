#!/usr/bin/env bash
# The team database both daemons serve from: one Docker Postgres of its own,
# never the shared agency-dev-pg. Safe to rerun; rerun it after installing an
# agency binary with new migrations (`make install-agency`).
#
#   1. The 1Password item "Agency team daemons" (Engineering vault): the
#      daemons' shared bearer tokens and run-claim key, and both database
#      logins. Created once, with generated values, from a JSON template on
#      stdin; never rewritten. No value is printed, logged or put on argv.
#   2. The container agency-team-pg (postgres:16.15-alpine, 127.0.0.1:5460, named
#      volume agency-team-pgdata, restarted by Docker), database agency_team.
#      Its superuser agency_team_admin migrates and owns; created on first
#      run, started when stopped.
#   3. Agency's own provisioning (scripts/provision_database.sh from the
#      installed agency, the source of the binary it migrates with): migrate,
#      agency-tools tables, grants, lifecycle_provision.sql, migration-verify.
#      The daemons serve as agency_team_writer, an ordinary login.
#
# 1Password access: the service account whose token is in the login-keychain
# item OP_SERVICE_ACCOUNT_KEYCHAIN_ITEM (the one the daemons use).
set -euo pipefail

RUNTIME=${AGENCY_RUNTIME_DIR:-/Volumes/SSD/agency-runtime}
KEYCHAIN_ITEM=${OP_SERVICE_ACCOUNT_KEYCHAIN_ITEM:-sc0red-agency-op-service-account}
VAULT=Engineering
ITEM="Agency team daemons"
CONTAINER=agency-team-pg
# Pinned: the volume's data directory belongs to this major version.
IMAGE=postgres:16.15-alpine
VOLUME=agency-team-pgdata
HOST_PORT=5460
DATABASE=agency_team
ADMIN=agency_team_admin
WRITER=agency_team_writer

fail() { printf 'team-db: %s\n' "$*" >&2; exit 1; }

[ -x "$RUNTIME/agency/agency" ] || fail "no agency installed at $RUNTIME/agency; run make install-agency"
OP_SERVICE_ACCOUNT_TOKEN=$(security find-generic-password -s "$KEYCHAIN_ITEM" -w 2>/dev/null) \
  || fail "no login-keychain item $KEYCHAIN_ITEM"
export OP_SERVICE_ACCOUNT_TOKEN

field() { op read "op://$VAULT/$ITEM/$1"; }

# Created only when 1Password says there is no such item; any other failure
# (network, token) stops here with op's own message.
if ! lookup=$(op item get "$ITEM" --vault "$VAULT" --format json 2>&1 >/dev/null); then
  case "$lookup" in *"isn't an item"*) ;; *) fail "1Password lookup of \"$ITEM\" failed: $lookup" ;; esac
  echo "team-db: creating the 1Password item \"$ITEM\" in $VAULT"
  python3 - "$ITEM" "$ADMIN" "$WRITER" "$HOST_PORT" "$DATABASE" <<'PY' | op item create --vault "$VAULT" - >/dev/null
import json, secrets, sys
title, admin, writer, port, database = sys.argv[1:]
admin_pw, writer_pw = secrets.token_hex(24), secrets.token_hex(24)
def url(login, password):
    return f"postgres://{login}:{password}@127.0.0.1:{port}/{database}"
values = {
    "agent_token": secrets.token_hex(32),
    "internal_event_token": secrets.token_hex(32),
    "run_claim_key": secrets.token_hex(32),
    "admin_password": admin_pw,
    "admin_database_url": url(admin, admin_pw),
    "writer_database_url": url(writer, writer_pw),
}
print(json.dumps({
    "title": title,
    "category": "SECURE_NOTE",
    "fields": [{"id": key, "label": key, "type": "CONCEALED", "value": value} for key, value in values.items()],
}))
PY
fi

if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
  echo "team-db: creating container $CONTAINER on 127.0.0.1:$HOST_PORT"
  # POSTGRES_PASSWORD is passed by name, so its value comes from this
  # process's environment, not argv.
  POSTGRES_PASSWORD=$(field admin_password) docker run -d --name "$CONTAINER" \
    --restart unless-stopped -p "127.0.0.1:$HOST_PORT:5432" -v "$VOLUME:/var/lib/postgresql/data" \
    -e POSTGRES_PASSWORD -e POSTGRES_USER="$ADMIN" -e POSTGRES_DB="$DATABASE" "$IMAGE" >/dev/null
elif [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER")" != "true" ]; then
  echo "team-db: starting container $CONTAINER"
  docker start "$CONTAINER" >/dev/null
fi
for _ in $(seq 1 60); do
  docker exec "$CONTAINER" pg_isready -U "$ADMIN" -d "$DATABASE" >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$CONTAINER" pg_isready -U "$ADMIN" -d "$DATABASE" >/dev/null || fail "$CONTAINER is not accepting connections after 60s"

AGENCY_MIGRATION_DATABASE_URL=$(field admin_database_url)
AGENCY_EVENTS_DATABASE_URL=$(field writer_database_url)
export AGENCY_MIGRATION_DATABASE_URL AGENCY_EVENTS_DATABASE_URL
# agency-tools from the release the daemons run, resolved once by
# run_on_release.sh, as for a daemon.
AGENCY_BIN="$RUNTIME/agency/agency" \
  AGENCY_TOOLS_ROOT="$RUNTIME/current/agency-tools" \
  AGENCY_PYTHON_BINARY="$RUNTIME/current/agency-tools/.venv/bin/python" \
  AGENCY_CONTRACTS_DIR="$RUNTIME/current/contracts" \
  PSQL="docker exec -i $CONTAINER psql -U $ADMIN -d $DATABASE" \
  "$RUNTIME/bin/run_on_release.sh" "$RUNTIME/agency/scripts/provision_database.sh"
echo "team-db: $DATABASE on 127.0.0.1:$HOST_PORT is migrated and provisioned; the daemons serve as $WRITER"
