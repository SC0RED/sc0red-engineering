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
- `workspaces/<agent>/agent.env`: daemon settings the launchd job loads (`MAX_CONCURRENT_RUNS`: Patch 2, Scarlett 1; `REDIS_URL`, required, a database of each agent's own, since Agency's keys are not namespaced by agent; the agency-tools and contracts paths below).
- Every stage rule runs at most one job per card: `singleFlightKey: '{{ repo }}#{{ issue }}'`, `singleFlightFailClosed: true`, and a `singleFlightTtlSecs` of at least `runTimeoutSecs` plus the sweep's 30-minute queue wait. A second dispatch of a stage for a card (two overlapping sweeps) waits behind that stage's live run, then stops when the card has left its column (Scarlett's plan review also stops when her verdict names the plan comment's current revision, its id and `updated_at`). The lock is per stage rule, not across stages; `board.open_for_review`'s registration relies on it because only the stage of the card's current column can register.
- The `board-check` rule in `workspaces/patch/agency.yaml` names each board to sweep as the `board.sweep` `board` argument, a key in `boards.json`, which holds that board's config (project, repos, which column dispatches which agent and stage, run slots); who a block mentions is `BOARD_NOTIFY_LOGIN` in each `agent.env`. The sweep dispatches each stage with context `{project, repo, issue, column, attempt}`, plus `pull_number` for both Code Review turns.

## The agency-tools the daemons run

Both daemons run the agency-tools and contracts the box runs, never a dev checkout. `scripts/sync_agency_tools.sh` installs the newest successful "Deploy Agency runtime" run on SC0RED/Agency into `/Volumes/SSD/agency-runtime`. It takes agency-tools at that run's `build.info` `at_sha` (a detached worktree, `uv sync --frozen --no-dev --no-editable`) and the run's `contracts.tar`, both checked against the artifact's `SHA256SUMS`. `current` is swapped atomically only after the release imports `board.sweep` and `scarlett.post_review`. Any failure leaves `current` as it was and exits 1.

- **The sync job:** `make install-sync` loads the launchd job that runs the sync every 10 minutes and at load (log: `/Volumes/SSD/agency-runtime/sync.log`). `make sync-tools` kickstarts it. Only launchd runs the sync, and it never starts the job while a run is alive, so syncs never overlap and there is no lock.
- **Immutable releases:** each release is built in a temporary directory, marked `.complete` last, and renamed into `releases/`. An existing release is reused, never rebuilt. A release is deleted only when it is neither `current` nor the previous release and was built over 7 days ago.
- **One release per daemon:** `agent.env` names `AGENCY_TOOLS_ROOT`, `AGENCY_PYTHON_BINARY` and `AGENCY_CONTRACTS_DIR` under `current`. Each daemon's launchd job starts it through `scripts/run_on_release.sh`, which resolves `current` once, at spawn, to the real release path, rewrites those three variables to it and execs the daemon. A sync never changes the code under a running daemon; a restart picks up the new release. There are no pins, so restart the daemons within 7 days of a newer release going live.
- **Rollback:** point `current` at the previous release and restart the daemons. The sync leaves it there until a newer successful deploy run appears and is installed, so the lasting fix is a new deploy.

Shared discipline (the constitution, engineering loop, shipping, review core, issue guides) lives in engineering-standards' `shared/`. `workspaces/shared/` vendors exactly the docs the templates cite, unedited, and `systemDocRoots: [..]` resolves `{{system-doc:shared/...}}` against it. To pick up a change in engineering-standards, run `make vendor-shared` and commit the result; never edit the copy here.

## Checks

Bare `make` runs every check:

- `check-structure`: no symlinks, `check_workspace.py` (with its self-test), and `test-sync` (the sync and `run_on_release.sh` against a fake `gh`, a fake `uv` and a local agency-tools origin, in temporary directories only). Actions runs this subset.
- `check-system-docs`: each vendored doc in `workspaces/shared/` is byte-identical to `$(ENGINEERING_STANDARDS)/shared`.
- `validate-workspace`: `agency validate-workspace` on each workspace, staged beside the vendored `shared/`, with `AGENCY_BIN` built from Agency `development`.
- `tool-refs`: agency-tools' `validate_workspace.py`, so every `module.python` reference resolves in `$(AGENCY_TOOLS)` (by default the synced release under `/Volumes/SSD/agency-runtime/current`).
