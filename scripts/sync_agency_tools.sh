#!/usr/bin/env bash
# Install, on this Mac, the agency-tools the box is running: the matched set of
# the newest successful "Deploy Agency runtime" run on SC0RED/Agency.
#
#   Run by launchd only (com.sc0red.agency-tools-sync: every 10 minutes and at
#   load). `make sync-tools` kickstarts that job. launchd never starts a job
#   while its previous run is alive, so two syncs never overlap.
#
# That run's agency-binary artifact carries build.info (at_sha: the
# agency-tools commit the box installed) and contracts.tar (the runtime's
# contracts agency-tools reads). A run that failed is never picked, so a deploy
# the box restored is never installed here.
#
# Layout under $AGENCY_RUNTIME_DIR (default /Volumes/SSD/agency-runtime):
#   agency-tools.git/          bare clone of SC0RED/agency-tools
#   releases/<at_sha>-<agency_sha12>/agency-tools/   detached worktree + .venv
#   releases/<...>/contracts/  the run's contracts
#   releases/<...>/.complete   written last; a release with it is never touched
#   current -> releases/<...>  swapped atomically once the release is verified
#   state/                     run id installed, previous release
#
# Releases are immutable. Each is built in releases/.build.*, marked complete,
# then renamed into place, so a release under releases/<name> is always whole
# and an existing one is reused, never rebuilt. A release is deleted only when
# it is neither current nor previous and was built over 7 days ago.
#
# A daemon never reads `current` after it starts: scripts/run_on_release.sh
# resolves it once, at spawn, to the real release path.
#
# Nothing here is copied by hand: the tools are a git worktree at the exact
# sha, the Python env is `uv sync --frozen` from its uv.lock (non-editable, so
# the env holds no path into the build directory), and the contracts are the
# run's own, checked against its SHA256SUMS. If any step fails, or the release
# does not import, `current` is left as it was and the script exits 1.
set -euo pipefail
umask 022

RUNTIME=${AGENCY_RUNTIME_DIR:-/Volumes/SSD/agency-runtime}
AGENCY_REPO=${AGENCY_REPO:-SC0RED/Agency}
DEPLOY_WORKFLOW=${DEPLOY_WORKFLOW:-deploy-runtime.yml}
TOOLS_REMOTE=${AGENCY_TOOLS_REMOTE:-https://github.com/SC0RED/agency-tools.git}
VERIFY_MODULES=${SYNC_VERIFY_MODULES:-agency_tools.board.sweep agency_tools.scarlett.post_review}
KEEP_DAYS=7
GH=${GH:-gh}
UV=${UV:-uv}
export UV_CACHE_DIR=${UV_CACHE_DIR:-/Volumes/SSD/.uv-cache}

log() { printf '%s sync-agency-tools: %s\n' "$(date -u +%FT%TZ)" "$*"; }
die() { log "$*; current left as it was"; exit 1; }

releases="$RUNTIME/releases"
bare="$RUNTIME/agency-tools.git"
mkdir -p "$releases" "$RUNTIME/state"

# A build left by a run that died. Only one sync runs at a time, so any
# .build.* here is abandoned.
for stale in "$releases"/.build.*; do
  [ -e "$stale" ] || continue
  rm -rf "$stale"
  log "removed abandoned $(basename "$stale")"
done
[ -d "$bare" ] && git -C "$bare" worktree prune

work=$(mktemp -d "$releases/.build.XXXXXX")
trap 'rm -rf "$work"' EXIT

complete() { [ -f "$1/.complete" ]; }

# A release imports as the daemons load it: its python, the tools root on
# PYTHONPATH, its contracts, from a neutral directory.
verify() {
  local dir=$1 module
  for module in $VERIFY_MODULES; do
    (cd "$work" && PYTHONPATH="$dir/agency-tools" AGENCY_CONTRACTS_DIR="$dir/contracts" \
      "$dir/agency-tools/.venv/bin/python" -c "import $module") || return 1
  done
}

# The box's matched set: the newest successful deploy run.
run_id=$("$GH" run list -R "$AGENCY_REPO" --workflow "$DEPLOY_WORKFLOW" --status success \
  --limit 1 --json databaseId --jq '.[0].databaseId // empty') || die "cannot list $AGENCY_REPO deploy runs"
[ -n "$run_id" ] || die "no successful $DEPLOY_WORKFLOW run on $AGENCY_REPO"

installed_run=$(cat "$RUNTIME/state/run-id" 2>/dev/null || true)
if [ "$run_id" = "$installed_run" ] && complete "$RUNTIME/current"; then
  log "run $run_id already installed ($(readlink "$RUNTIME/current"))"
  exit 0
fi

artifact="$work/artifact"
"$GH" run download "$run_id" -R "$AGENCY_REPO" -n agency-binary -D "$artifact" >/dev/null \
  || die "cannot download run $run_id's agency-binary artifact"
for file in build.info contracts.tar SHA256SUMS; do
  [ -f "$artifact/$file" ] || die "run $run_id's artifact has no $file"
done
# Exactly one SHA256SUMS line per file, each checked against that file.
: > "$work/sums"
for file in build.info contracts.tar; do
  lines=$(grep -E '^[0-9a-f]{64}  ' "$artifact/SHA256SUMS" | awk -v f="$file" 'NF == 2 && $2 == f' || true)
  [ -n "$lines" ] && [ "$(printf '%s\n' "$lines" | wc -l | tr -d ' ')" = 1 ] \
    || die "run $run_id's SHA256SUMS does not list $file exactly once"
  printf '%s\n' "$lines" >> "$work/sums"
done
(cd "$artifact" && shasum -a 256 -c --status "$work/sums") \
  || die "run $run_id's build.info or contracts.tar does not match its SHA256SUMS"

info() { sed -n "s/^$1=//p" "$artifact/build.info" | head -1; }
at_sha=$(info at_sha)
agency_sha=$(info sha)
[[ "$at_sha" =~ ^[0-9a-f]{40}$ ]] || die "run $run_id's build.info has no agency-tools sha (at_sha=$at_sha)"
[[ "$agency_sha" =~ ^[0-9a-f]{40}$ ]] || die "run $run_id's build.info has no Agency sha (sha=$agency_sha)"

name="$at_sha-${agency_sha:0:12}"
release="$releases/$name"
current_rel=$(readlink "$RUNTIME/current" 2>/dev/null || true)
previous_rel=$(cat "$RUNTIME/state/previous" 2>/dev/null || true)

git_() { git -c core.hooksPath=/dev/null -c credential.helper= -c "credential.helper=!$GH auth git-credential" "$@"; }

if complete "$release"; then
  log "reusing release $name"
else
  if [ -e "$release" ]; then
    # Not marked complete: never a release this script finished. Remove it
    # unless something points at it.
    case "releases/$name" in
      "$current_rel" | "$previous_rel") die "release $name is current or previous but not complete" ;;
    esac
    rm -rf "$release"
  fi
  [ -d "$bare" ] || git_ clone --quiet --bare "$TOOLS_REMOTE" "$bare" || die "cannot clone $TOOLS_REMOTE"
  git_ -C "$bare" fetch --quiet origin "$at_sha" || die "cannot fetch agency-tools $at_sha"

  build="$work/release"
  mkdir "$build"
  git_ -C "$bare" worktree add --quiet --detach "$build/agency-tools" "$at_sha" \
    || die "cannot check out agency-tools $at_sha"
  tar -xf "$artifact/contracts.tar" -C "$build" || die "cannot unpack run $run_id's contracts"
  [ -d "$build/contracts" ] || die "run $run_id's contracts.tar has no contracts/"
  (cd "$build/agency-tools" && "$UV" sync --quiet --frozen --no-dev --no-editable) \
    || die "uv sync --frozen failed for agency-tools $at_sha"
  [ -x "$build/agency-tools/.venv/bin/python" ] || die "uv sync left no .venv/bin/python for $at_sha"
  verify "$build" || die "release $name cannot import one of: $VERIFY_MODULES"
  chmod -R a+rX "$build"
  printf 'run=%s\nat_sha=%s\nsha=%s\n' "$run_id" "$at_sha" "$agency_sha" > "$build/.complete"
  mv "$build" "$release" || die "cannot move release $name into place"
  git -C "$bare" worktree repair "$release/agency-tools" >/dev/null 2>&1 || true
  log "built release $name"
fi

# Check the release in place, whether just built or reused, before it goes live.
head=$(git -C "$release/agency-tools" rev-parse HEAD 2>/dev/null || true)
[ "$head" = "$at_sha" ] || die "release $name is at '$head', not $at_sha"
verify "$release" || die "release $name cannot import one of: $VERIFY_MODULES"

# Swap: one rename of a symlink, so a reader sees the old release or the new.
if [ "$current_rel" != "releases/$name" ]; then
  ln -sfn "releases/$name" "$RUNTIME/.current.new"
  python3 -c 'import os, sys; os.replace(sys.argv[1], sys.argv[2])' "$RUNTIME/.current.new" "$RUNTIME/current"
  if [ -n "$current_rel" ]; then
    echo "$current_rel" > "$RUNTIME/state/previous"
    previous_rel=$current_rel
  fi
  log "installed run $run_id: agency-tools $at_sha, Agency $agency_sha (was ${current_rel:-nothing})"
else
  log "run $run_id is release $name, already current"
fi
echo "$run_id" > "$RUNTIME/state/run-id"

# Delete a release only when it is neither current nor previous and was built
# more than $KEEP_DAYS days ago.
for old in "$releases"/*; do
  [ -d "$old" ] || continue
  rel="releases/$(basename "$old")"
  case "$rel" in "releases/$name" | "$previous_rel") continue ;; esac
  if complete "$old" && [ -z "$(find "$old/.complete" -mmin +$((KEEP_DAYS * 24 * 60)))" ]; then
    continue
  fi
  git -C "$bare" worktree remove --force "$old/agency-tools" 2>/dev/null || true
  rm -rf "$old"
  log "removed $rel"
done
[ -d "$bare" ] && git -C "$bare" worktree prune
exit 0
