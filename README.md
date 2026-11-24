# sc0red-engineering

<img src="workspaces/patch/avatars/patch.jpg" alt="Patch" width="140" align="right">
<img src="workspaces/scarlett/avatars/scarlett.jpg" alt="Scarlett" width="140" align="right">

The Agency workspaces for sc0red's engineering agents. **Patch** (Claude) plans, builds, fixes and merges. **Scarlett** (Codex) reviews plans and pull requests. They work the cards on the GitHub Project boards: NeuroBloom (`neurobloom-lilly` project 2) and, later, Winston (`SC0RED` project 1).

## How work moves

The board is the state. Every two minutes, Patch's `board-check` schedule rule runs `agency_tools.board.sweep` in preRun and stops before any LLM run. The sweep claims cards in the agents' columns and dispatches each stage as a `routing.internal` task:

| Column | Agent | Stage (taskType) | Ends with |
|---|---|---|---|
| Plan → Planning | Patch | `plan` | `board.submit_plan` (→ Plan Review), `board.propose_split`, or `board.block` |
| Plan Review | Scarlett | `plan-review` | `board.post_verdict` (approve stays for the owner) |
| Ready for Development → In Development | Patch | `build` | `board.open_for_review` (→ Code Review) |
| Code Review | Scarlett | `code-review` | read-only review (diff, Astra, GitHub CI; she never runs the PR's code), then `scarlett/review/<base>` posted by `scarlett.post_review` (the gate's `scarlett-review`; only Scarlett's code-review stage holds it), or `board.block` with the findings |
| Code Review | Patch | `code-review` | merge through the gate once Scarlett's status is on the head, deploy watched, then `board.merged` |
| Hotfix | Patch | `hotfix` | two PRs (production fix and trunk back-merge), then `board.open_for_review`; the card moves on only when both have merged |

The owner's gate is the plan: he moves a card from Plan Review to Ready for Development himself, and no agent tool can make that move. PRs have no per-PR human approval; they merge when the gate's and GitHub's rules are met, and Scarlett's status stands for green CI (which runs bare `make`) and her Astra "merge" on that head. Only Scarlett posts it; the gate's merge checks that GitHub shows it.

Each stage's tool allowlist holds only that stage's board moves. Patch's own run token has no Projects permission, so the board tools are the only way a card moves. Every stage rule sets `runTimeoutSecs`, and a failed or timed-out run is re-dispatched once by the sweep, then blocked with the reason.

The full design is in `ENGINEERING_TEAM_DESIGN.md` (the lead's copy, beside this checkout).

## Layout

- `workspaces/<agent>/agency.yaml`: the rules. `scripts/check_workspace.py` pins each stage's allowlist, so a change to what a stage may do changes that table in the same PR.
- `workspaces/patch/boards.json`: the board config, by board name: project, repos (each with the `production_branch` a hotfix's fix must target), stages and run slots. The sweep step in `agency.yaml` names a board here, and `board.open_for_review` and `board.merged` read the same file for a repo's production branch, so no agent ever supplies it.
- `workspaces/<agent>/templates/`: one prompt per stage. Each opens with `## Intent` and cites engineering-standards rather than restating it.
- `workspaces/<agent>/identity/`: SOUL and IDENTITY.
- `workspaces/<agent>/agent.env`: daemon settings the launchd job loads (`MAX_CONCURRENT_RUNS`: Patch 2, Scarlett 2, each the sum of that agent's `slots` over every board in `boards.json`, since the sweep counts slots per board; `REDIS_URL`, required, a database of each agent's own, since Agency's keys are not namespaced by agent; the agency-tools and contracts paths below).
- Every stage rule runs at most one job per card: `singleFlightKey: '{{ repo }}#{{ issue }}'`, `singleFlightFailClosed: true`, and a `singleFlightTtlSecs` of at least `runTimeoutSecs` plus the sweep's 30-minute queue wait. A second dispatch of a stage for a card (two overlapping sweeps) waits behind that stage's live run, then stops when the card has left its column (Scarlett's plan review also stops when her verdict names the plan comment's current revision, its id and `updated_at`). The lock is per stage rule, not across stages; `board.open_for_review`'s registration relies on it because only the stage of the card's current column can register.
- The `board-check` rule in `workspaces/patch/agency.yaml` names each board to sweep as the `board.sweep` `board` argument, a key in `boards.json`, which holds that board's config (project, repos, which column dispatches which agent and stage, run slots); who a block mentions is `BOARD_NOTIFY_LOGIN` in each `agent.env`. The sweep dispatches each stage with context `{project, repo, issue, column, attempt}`, plus `pull_number` for both Code Review turns.

## The agency-tools the daemons run

Both daemons run the agency-tools and contracts the box runs, never a dev checkout. `scripts/sync_agency_tools.sh` installs the newest successful "Deploy Agency runtime" run on SC0RED/Agency into `/Volumes/SSD/agency-runtime`. It takes agency-tools at that run's `build.info` `at_sha` (a detached worktree, `uv sync --frozen --no-dev --no-editable`) and the run's `contracts.tar`, both checked against the artifact's `SHA256SUMS`. `current` is swapped atomically only after the release imports `board.sweep` and `scarlett.post_review`. Any failure before the swap leaves `current` as it was and exits 1; a failure after it (recording the run id, pruning) exits 1 with the new release already current and whole.

- **The sync job:** `make install-sync` copies `sync_agency_tools.sh` and `run_on_release.sh` into `/Volumes/SSD/agency-runtime/bin/` (rerun it after changing either) and loads the launchd job that runs that copy (never this checkout) every 10 minutes and at load (log: `/Volumes/SSD/agency-runtime/sync.log`). `make sync-tools` kickstarts it. Only launchd runs the sync, and it never starts the job while a run is alive, so syncs never overlap and there is no lock. One-time setup: give `/bin/bash` Full Disk Access (System Settings → Privacy & Security → Full Disk Access → +, ⌘⇧G `/bin/bash`). macOS blocks launchd jobs from the external SSD otherwise, so the job opens its log through bash, never through launchd's own `StandardOutPath`.
- **Immutable releases:** each release is built in a temporary directory, marked `.complete` last, and renamed into `releases/`. An existing release is reused, never rebuilt. A release is deleted only when it is neither `current` nor the previous release and both its build and its last time in use (as `current` or previous, recorded in `state/in-use/`) are over 7 days ago. The previous release is recorded before `current` swaps, so an interrupted swap leaves no release unprotected.
- **One release per daemon:** `agent.env` names `AGENCY_TOOLS_ROOT`, `AGENCY_PYTHON_BINARY` and `AGENCY_CONTRACTS_DIR` under `current`. Each daemon's launchd job starts it through `scripts/run_on_release.sh`, which resolves `current` once, at spawn, to the real release path, rewrites those three variables to it and execs the daemon. A sync never changes the code under a running daemon; a restart picks up the new release. There are no pins, so restart the daemons within 7 days of their release leaving `current` and previous.
- **Rollback:** point `current` at the previous release and restart the daemons. The sync leaves it there until a newer successful deploy run appears and is installed, so the lasting fix is a new deploy.

## The daemons on this Mac

Each agent is one `agency` daemon under launchd in the owner's login session (`gui/<uid>`), label `com.sc0red.agency-<agent>`, rendered from `launchd/com.sc0red.agency-daemon.plist`. Patch serves on 127.0.0.1:8810, Scarlett on 127.0.0.1:8811 (`AGENCY_BIND_LOOPBACK=true`: loopback only) (her daemon is installed once the Codex tool bridge lands). Install in this order:

0. The Agency checkout must hold all three Agency changes this setup needs, merged to `development` in this order: SC0RED/Agency#1058 (`scripts/provision_database.sh`, which `install-agency` copies and `team-db` runs), then SC0RED/Agency#1064 (runs never hold the 1Password token; `op` runs with a home of Agency's own, so a macOS privacy prompt on the 1Password app can't hang it), then SC0RED/Agency#1065 (`AGENCY_BIND_LOOPBACK`). Until all three are on `development`, build from a checkout that merges them. An older Agency fails `install-agency` (no `provision_database.sh`), or listens on every interface with runs holding the token.
1. `make install-agency AGENCY_SRC=<Agency checkout>`: the release binary (`cargo build --release` in that checkout) plus the Agency scripts that go with it (`agency_mcp_server.py`, `provision_database.sh`, `lifecycle_provision.sql`) into `/Volumes/SSD/agency-runtime/agency/`, swapped in by renames; `build.info` records the sha.
2. `make team-db`: the team database, idempotent (`scripts/team_db.sh`). The 1Password item "Agency team daemons" (Engineering) is created once with generated values: the daemons' shared `AGENCY_AGENT_TOKEN`, `AGENCY_INTERNAL_EVENT_TOKEN` and `AGENCY_RUN_CLAIM_KEY`, and both database logins. The Docker container `agency-team-pg` (postgres:16.15-alpine, `127.0.0.1:5460`, volume `agency-team-pgdata`, `--restart unless-stopped`) holds database `agency_team`; its superuser `agency_team_admin` migrates, and Agency's own `provision_database.sh` provisions it. The daemons serve as `agency_team_writer`, an ordinary login. Rerun after an install-agency that brings migrations.
3. `make install-daemon AGENT=patch`: the workspace beside the vendored `shared/` in `/Volumes/SSD/agency-runtime/workspaces/`, the launcher `scripts/start_daemon.sh`, and the launchd job, (re)loaded. A loaded job is booted out first, which waits for the daemon to drain its runs (up to 5 minutes).
4. `make daemon-health AGENT=patch`; `make test-survival AGENT=patch` (layer 2 below).

How it runs:

- **Launch:** launchd runs `/bin/bash -c 'exec /bin/bash start_daemon.sh <agent> >>log 2>&1'`, so bash, not launchd, opens the log on the SSD. `start_daemon.sh` sources the installed `agent.env`, reads the 1Password service-account token from the login-keychain item `OP_SERVICE_ACCOUNT_KEYCHAIN_ITEM` names, and execs the daemon through `run_on_release.sh`, so the job's pid is the daemon's. KeepAlive restarts it (at most every 30 s); on stop launchd sends SIGTERM, allows 330 s for the drain, then kills what is left in the job's own process group.
- **Orphaned runs (known risk):** Agency starts claude and its python tools in process groups of their own, and launchd, unlike systemd's cgroup on the Linux boxes, kills only the job's group. A normal stop drains runs first, but if the daemon crashes or is SIGKILLed, a run in flight keeps going under no daemon, and the restarted daemon can start the same stage beside it. Until Agency reaps its own children on macOS (SC0RED/Agency#1057), after a daemon crash check `pgrep -lf 'claude -p'` for claude processes whose parent is gone, and stop them by PID.
- **Per daemon, on the SSD:** `/Volumes/SSD/agency-runtime/<agent>/agency.log`, `audit.log` (`AGENCY_AUDIT_LOG_PATH`; each tool call and its result, the board sweep's included) and `events-buffer.jsonl` (`AGENCY_EVENTS_BUFFER_PATH`, where events wait while Postgres is down).
- **No inbound network:** `AGENCY_BIND_LOOPBACK=true`, so the daemon listens on 127.0.0.1 only (needs an agency with Agency PR "op token and bind address").
- **Secrets:** `AGENCY_OP_BINDINGS_FILE` is `workspaces/<agent>/op-bindings.json`, key to `op://` reference, never a value. Agency's SecretManager resolves them with the service account "The Agency" (Engineering vault), kept in the login keychain as `sc0red-agency-op-service-account`, so the 1Password app's lock never matters. Agency withholds that token from every run and tool it starts; only its own `op` child gets it.
- **TLS:** the tools' python (the python.org framework build) has no CA bundle, so `agent.env` sets `SSL_CERT_FILE=/etc/ssl/cert.pem`.

**The owner's manual steps** (layer 2 parts a script must not do):

- **Reboot:** the login keychain stays locked until you log in, so the daemon can't read its service-account token: `start_daemon.sh` exits 1 and launchd retries every 30 s. Once you log in, check `make daemon-health AGENT=patch` and that `audit.log` shows a new `board.sweep` within 2 minutes. Docker Desktop must start at login too (it restarts `agency-team-pg`).
- **Sleep attempt:** the Mac is set never to sleep; try it anyway (Apple menu → Sleep), wake it, and check the same two things.
- **Locked 1Password app:** lock it and check that the next sweep still runs (the daemons use the service account, never the app).
- **Separation of duties:** every service account we have reads the whole Engineering vault, so Patch's daemon can read Scarlett's App key. The design's fix is the owner's: a vault and service account per agent, and each daemon under its own macOS user.

Shared discipline (the constitution, engineering loop, shipping, review core, issue guides) lives in engineering-standards' `shared/`. `workspaces/shared/` vendors exactly the docs the templates cite, unedited, and `systemDocRoots: [..]` resolves `{{system-doc:shared/...}}` against it. To pick up a change in engineering-standards, run `make vendor-shared` and commit the result; never edit the copy here.

## Checks

Bare `make` runs every check:

- `check-structure`: no symlinks, `check_workspace.py` (with its self-test), and `test-sync` (the sync and `run_on_release.sh` against a fake `gh`, a fake `uv` and a local agency-tools origin, in temporary directories only). Actions runs this subset.
- `check-system-docs`: each vendored doc in `workspaces/shared/` is byte-identical to `$(ENGINEERING_STANDARDS)/shared`.
- `validate-workspace`: `agency validate-workspace` on each workspace, staged beside the vendored `shared/`, with `AGENCY_BIN` built from Agency `development`.
- `tool-refs`: agency-tools' `validate_workspace.py`, so every `module.python` reference resolves in `$(AGENCY_TOOLS)` (by default the synced release under `/Volumes/SSD/agency-runtime/current`).
