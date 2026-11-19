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
| Code Review | Scarlett | `code-review` | `scarlett/review/<base>` posted by the gate's `scarlett-review`, or a findings comment |
| Code Review | Patch | `code-review` | merge through the gate, deploy watched, then `board.merged` |
| Hotfix | Patch | `hotfix` | `board.open_for_review` |

The owner's gate is the plan: he moves a card from Plan Review to Ready for Development himself, and no agent tool can make that move. PRs have no per-PR human approval; they merge when the gate's and GitHub's rules are met, and Scarlett's status stands for a clean `make` and an Astra "merge" on that head.

Each stage's tool allowlist holds only that stage's board moves. Patch's own run token has no Projects permission, so the board tools are the only way a card moves. Every stage rule sets `runTimeoutSecs`, and a failed or timed-out run is re-dispatched once by the sweep, then blocked with the reason.

The full design is in `ENGINEERING_TEAM_DESIGN.md` (the lead's copy, beside this checkout).

## Layout

- `workspaces/<agent>/agency.yaml`: the rules. `scripts/check_workspace.py` pins each stage's allowlist, so a change to what a stage may do changes that table in the same PR.
- `workspaces/<agent>/templates/`: one prompt per stage. Each opens with `## Intent` and cites engineering-standards rather than restating it.
- `workspaces/<agent>/identity/`: SOUL and IDENTITY.
- `workspaces/<agent>/agent.env`: daemon settings the launchd job loads (`MAX_CONCURRENT_RUNS`: Patch 2, Scarlett 1).
- `workspaces/patch/boards.yaml`: the boards, their repos and trunks, and which column belongs to which agent and stage. The board tools read it.

Shared discipline (the constitution, engineering loop, shipping, review core, issue guides) is not copied here. engineering-standards stamps its `shared/` directory beside the workspaces, and `systemDocRoots: [../shared]` resolves `{{system-doc:...}}` against it.

## Checks

Bare `make` runs every check:

- `check-structure`: no symlinks, and `check_workspace.py` (with its self-test). Actions runs this subset.
- `check-system-docs`: every `{{system-doc:...}}` resolves against `$(ENGINEERING_STANDARDS)/shared`.
- `validate-workspace`: `agency validate-workspace` on each workspace, staged beside engineering-standards' `shared/`, with `AGENCY_BIN` built from Agency `development`.
- `tool-refs`: agency-tools' `validate_workspace.py`, so every `module.python` reference resolves in `$(AGENCY_TOOLS)`.
